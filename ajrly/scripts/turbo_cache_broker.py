#!/usr/bin/env python3
"""Request Turbo grants from ci-cache-authority after exact source checkout.

Ordinary jobs never receive the upstream Turbo writer credential. When the
broker is enabled, a job-local proxy binds Turbo's static TURBO_TOKEN model to
short-lived authority HMAC grants:

  TURBO_API   -> http://127.0.0.1:<port>  (job-local proxy)
  TURBO_TOKEN -> static local bearer (compatible with real Turbo executables)

The local proxy re-POSTs /v1/authorize/* with the live CI_JOB_TOKEN when the
authority grant is near expiry or returns 401 grant expired. Authority keeps
GRANT_TTL=600 and revalidates GitLab job liveness on every proxy hop.
Naive one-shot configure that only exports an HMAC TURBO_TOKEN cannot refresh
Turbo mid-job — the real client holds a static token for live requests.

Defaults keep the broker disabled until root canary. Caller flags / source
claims never authorize or refresh grants.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlparse


DEFAULT_BROKER_URL = (
    "http://ci-cache-authority.gitlab-runners.svc.cluster.local:8090"
)
FORBIDDEN_UPSTREAM_HOSTS = (
    "turbo-cache-api.artifacts.svc",
    "turbo-cache.ajrly.com",
)
# Refresh before hard expiry so mid-artifact uploads are not cut off.
REFRESH_SKEW_SECONDS = 60
LOCAL_PROXY_HOST = "127.0.0.1"
# Match authority TEAM; Turbo 2.11.7 sends TURBO_TEAM as ?slug=.
DEFAULT_TEAM = "ajrly"
ALLOWED_ARTIFACT_QUERY_KEYS = frozenset({"teamid", "slug"})
MAX_PROXY_BODY_BYTES = 64 * 1024 * 1024
PROXY_REQUEST_TIMEOUT = 60.0
AUTHORITY_REQUEST_TIMEOUT = 15.0


@dataclass(frozen=True)
class BrokerConfig:
    enabled: bool
    write_enabled: bool
    broker_url: str
    scope: str
    denial: str = ""


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def parse_config(env: Mapping[str, str]) -> BrokerConfig:
    enabled = _truthy(env.get("AJRLY_TURBO_BROKER_ENABLED"))
    write_enabled = _truthy(env.get("AJRLY_TURBO_WRITE_ENABLED"))
    broker_url = (env.get("AJRLY_TURBO_BROKER_URL") or DEFAULT_BROKER_URL).rstrip("/")
    if not enabled:
        return BrokerConfig(
            enabled=False,
            write_enabled=False,
            broker_url=broker_url,
            scope="read",
            denial="broker_disabled",
        )
    parsed = urlparse(broker_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return BrokerConfig(
            enabled=False,
            write_enabled=False,
            broker_url=broker_url,
            scope="read",
            denial="invalid_broker_url",
        )
    host = parsed.netloc.lower()
    if any(bad in host for bad in FORBIDDEN_UPSTREAM_HOSTS):
        return BrokerConfig(
            enabled=False,
            write_enabled=False,
            broker_url=broker_url,
            scope="read",
            denial="upstream_writer_endpoint_forbidden",
        )
    scope = "write" if write_enabled else "read"
    return BrokerConfig(
        enabled=True,
        write_enabled=write_enabled,
        broker_url=broker_url,
        scope=scope,
    )


def sanitize_artifact_query(query: str, *, team: str) -> str:
    """Mirror authority: only Turbo teamId/slug matching the grant team."""
    if not query:
        return ""
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    if not pairs:
        raise ValueError("artifact query string rejected")
    allowed: list[tuple[str, str]] = []
    for key, value in pairs:
        lowered = key.lower()
        if lowered not in ALLOWED_ARTIFACT_QUERY_KEYS:
            raise ValueError("artifact query parameter rejected")
        if value != team:
            raise ValueError("artifact team query mismatch")
        out_key = "teamId" if lowered == "teamid" else "slug"
        allowed.append((out_key, value))
    return urllib.parse.urlencode(allowed)


def request_grant(
    *,
    broker_url: str,
    job_token: str,
    scope: str,
    opener: urllib.request.OpenerDirector | None = None,
    body: dict | None = None,
) -> dict:
    if not job_token.strip():
        raise ValueError("CI_JOB_TOKEN required")
    # Caller-supplied job_id/sha/ref/mode/trust flags must never refresh grants.
    if body:
        for forged in (
            "job_id",
            "project_id",
            "pipeline_id",
            "sha",
            "ref",
            "mode",
            "source_sha",
            "trusted",
            "AJRLY_SOURCE_VERIFIED",
            "expires_at",
            "grant_id",
            "token",
        ):
            body.pop(forged, None)
    safe_body = {"audience": "turbo-cache-proxy"}
    path = "/v1/authorize/write" if scope == "write" else "/v1/authorize/read"
    url = broker_url.rstrip("/") + path
    payload = json.dumps(safe_body).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "JOB-TOKEN": job_token,
            "Authorization": "Job-Token " + job_token,
        },
    )
    open_fn = opener.open if opener is not None else urllib.request.urlopen
    try:
        with open_fn(req, timeout=AUTHORITY_REQUEST_TIMEOUT) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        _detail = error.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"authority grant failed status={error.code}") from None
    grant = json.loads(raw.decode())
    if not isinstance(grant, dict) or not grant.get("token"):
        raise RuntimeError("authority grant missing token")
    dumped = json.dumps(grant)
    if "upstream" in dumped.lower() and "upstream-writer" in dumped.lower():
        raise RuntimeError("authority response leaked upstream writer credential")
    for banned in ("password", "AUTH_ROOT", "root_password"):
        if banned.lower() in dumped.lower():
            raise RuntimeError("authority response leaked admin credential")
    return grant


class GrantSession:
    """Holds the current authority grant and refreshes with live JOB-TOKEN only."""

    def __init__(
        self,
        *,
        broker_url: str,
        job_token: str,
        scope: str,
        opener: urllib.request.OpenerDirector | None = None,
        now: Callable[[], float] | None = None,
        skew_seconds: int = REFRESH_SKEW_SECONDS,
    ) -> None:
        self.broker_url = broker_url
        self.job_token = job_token
        self.scope = scope
        self.opener = opener
        self.now = now or time.time
        self.skew_seconds = skew_seconds
        self._grant: dict | None = None
        self._lock = threading.Lock()

    def needs_refresh(self, grant: dict | None = None) -> bool:
        current = self._grant if grant is None else grant
        if not current or not current.get("token"):
            return True
        expires_at = int(current.get("expires_at") or 0)
        return expires_at - int(self.now()) <= self.skew_seconds

    def current_grant(self, *, force: bool = False) -> dict:
        with self._lock:
            if force or self.needs_refresh(self._grant):
                self._grant = request_grant(
                    broker_url=self.broker_url,
                    job_token=self.job_token,
                    scope=self.scope,
                    opener=self.opener,
                )
            assert self._grant is not None
            return dict(self._grant)

    def invalidate(self) -> None:
        with self._lock:
            self._grant = None


def export_env(
    grant: dict,
    config: BrokerConfig,
    *,
    turbo_api: str | None = None,
    turbo_token: str | None = None,
) -> dict[str, str]:
    """Export Turbo env. Prefer job-local proxy URL + static token when provided."""
    team = str(grant.get("team") or DEFAULT_TEAM)
    cache_mode = "local:rw,remote:rw" if config.scope == "write" else "local:rw,remote:r"
    api = turbo_api if turbo_api is not None else config.broker_url
    token = turbo_token if turbo_token is not None else str(grant["token"])
    return {
        "TURBO_API": api,
        "TURBO_TOKEN": token,
        "TURBO_TEAM": team,
        "TURBO_CACHE": cache_mode,
        # Explicitly clear legacy direct-read tokens so jobs cannot bypass proxy.
        "TURBO_CACHE_READ_TOKEN": "",
        "TURBO_CACHE_TOKEN": "",
    }


def shell_exports(values: Mapping[str, str]) -> str:
    lines = []
    for key, value in values.items():
        if value == "":
            lines.append(f"unset {key}")
        else:
            lines.append(f"export {key}={json.dumps(value)}")
    return "\n".join(lines) + "\n"


def _forward_to_authority(
    *,
    method: str,
    path: str,
    body: bytes,
    headers: Mapping[str, str],
    authority_base: str,
    grant_token: str,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[int, dict[str, str], bytes]:
    url = authority_base.rstrip("/") + path
    out_headers = {
        "Authorization": "Bearer " + grant_token,
        "Accept": headers.get("accept", "*/*"),
        "User-Agent": headers.get("user-agent", "ajrly-turbo-local-proxy/1.0"),
    }
    if method == "PUT" and body:
        out_headers["Content-Type"] = headers.get(
            "content-type", "application/octet-stream"
        )
        out_headers["Content-Length"] = str(len(body))
    req = urllib.request.Request(
        url, data=body if method == "PUT" else None, method=method, headers=out_headers
    )
    open_fn = opener.open if opener is not None else urllib.request.urlopen
    try:
        with open_fn(req, timeout=PROXY_REQUEST_TIMEOUT) as response:
            raw = response.read()
            resp_headers = {k.lower(): v for k, v in response.headers.items()}
            return int(response.status), resp_headers, raw
    except urllib.error.HTTPError as error:
        raw = error.read()
        resp_headers = {k.lower(): v for k, v in error.headers.items()}
        return int(error.code), resp_headers, raw


def make_local_proxy_handler(
    *,
    session: GrantSession,
    local_token: str,
    opener: urllib.request.OpenerDirector | None = None,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"
        # Bound idle clients; body size enforced in _read_body.
        timeout = PROXY_REQUEST_TIMEOUT

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            # Never echo full URLs (may include tokens in mistaken query forms).
            path = self.path.split("?", 1)[0]
            sys.stderr.write("local-turbo-proxy %s %s\n" % (self.command, path))

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_HEAD(self) -> None:  # noqa: N802
            self._dispatch()

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._json(403, {"error": "cache administration denied"})

        def do_DELETE(self) -> None:  # noqa: N802
            self._json(403, {"error": "cache administration denied"})

        def _json(self, status: int, payload: dict) -> None:
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(raw)

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length < 0 or length > MAX_PROXY_BODY_BYTES:
                raise ValueError("body too large")
            return self.rfile.read(length) if length else b""

        def _dispatch(self) -> None:
            try:
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                if path == "/health":
                    self._json(200, {"status": "ok"})
                    return
                auth = self.headers.get("Authorization") or ""
                if not auth.lower().startswith("bearer "):
                    self._json(401, {"error": "local bearer required"})
                    return
                presented = auth.split(" ", 1)[1].strip()
                if not secrets.compare_digest(presented, local_token):
                    self._json(401, {"error": "local bearer mismatch"})
                    return
                lowered = {k.lower(): v for k, v in self.headers.items()}
                for banned in (
                    "x-ci-cache-refresh",
                    "x-ajrly-trusted",
                    "x-ci-cache-source-sha",
                    "x-ci-cache-source-ref",
                    "x-ci-cache-job-id",
                    "x-grant-refresh",
                    "job-token",
                ):
                    if banned in lowered:
                        self._json(
                            403,
                            {
                                "error": "caller trust/refresh claims cannot renew grants"
                            },
                        )
                        return
                if not path.startswith("/v8/artifacts"):
                    self._json(403, {"error": "path not authorized for cache proxy"})
                    return
                body = self._read_body() if self.command == "PUT" else b""
                grant = session.current_grant()
                team = str(grant.get("team") or DEFAULT_TEAM)
                safe_query = sanitize_artifact_query(parsed.query, team=team)
                forward_path = path + (f"?{safe_query}" if safe_query else "")
                status, resp_headers, payload = _forward_to_authority(
                    method=self.command,
                    path=forward_path,
                    body=body,
                    headers=lowered,
                    authority_base=session.broker_url,
                    grant_token=str(grant["token"]),
                    opener=opener,
                )
                if status == 401 and b"grant expired" in payload.lower():
                    session.invalidate()
                    grant = session.current_grant(force=True)
                    team = str(grant.get("team") or DEFAULT_TEAM)
                    safe_query = sanitize_artifact_query(parsed.query, team=team)
                    forward_path = path + (f"?{safe_query}" if safe_query else "")
                    status, resp_headers, payload = _forward_to_authority(
                        method=self.command,
                        path=forward_path,
                        body=body,
                        headers=lowered,
                        authority_base=session.broker_url,
                        grant_token=str(grant["token"]),
                        opener=opener,
                    )
                self.send_response(status)
                for key, value in resp_headers.items():
                    if key in {"transfer-encoding", "connection", "content-length"}:
                        continue
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)
            except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as error:
                self._json(502, {"error": f"local proxy error: {error}"})

    return Handler


class LocalTurboProxy(ThreadingHTTPServer):
    daemon_threads = True
    # Cap concurrent Turbo artifact hops toward authority (matches server Semaphore 8).
    request_queue_size = 8

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def start_local_proxy(
    *,
    session: GrantSession,
    local_token: str,
    host: str = LOCAL_PROXY_HOST,
    port: int = 0,
    opener: urllib.request.OpenerDirector | None = None,
) -> LocalTurboProxy:
    handler = make_local_proxy_handler(
        session=session, local_token=local_token, opener=opener
    )
    server = LocalTurboProxy((host, port), handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    return server


def _state_dir(env: Mapping[str, str]) -> Path:
    root = Path(env.get("CI_JOB_TMPDIR") or tempfile.gettempdir())
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_private_json(path: Path, payload: dict) -> None:
    """Create mode 0600 before writing so the token is never briefly world-readable."""
    raw = (json.dumps(payload, sort_keys=True) + "\n").encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            fd = -1  # ownership transferred to fdopen
    finally:
        if fd >= 0:
            os.close(fd)


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def serve_from_state(state_path: Path) -> int:
    state = json.loads(state_path.read_text())
    config = BrokerConfig(
        enabled=True,
        write_enabled=state["scope"] == "write",
        broker_url=state["broker_url"],
        scope=state["scope"],
    )
    session = GrantSession(
        broker_url=config.broker_url,
        job_token=state["job_token"],
        scope=config.scope,
    )
    # Initial mint proves JOB-TOKEN is live before advertising ready.
    grant = session.current_grant()
    local_token = state["local_token"]
    server = start_local_proxy(session=session, local_token=local_token)
    turbo_api = f"http://{LOCAL_PROXY_HOST}:{server.server_port}"
    ready_path = Path(state["ready_path"])
    _write_private_json(
        ready_path,
        {
            "turbo_api": turbo_api,
            "turbo_token": local_token,
            "scope": config.scope,
            "grant_id": grant.get("grant_id"),
            "expires_at": grant.get("expires_at"),
            "mode": "job-local-proxy",
            "pid": os.getpid(),
        },
    )
    # Child stderr is redirected by configure; keep messages free of tokens.
    print(
        json.dumps(
            {
                "listening": turbo_api,
                "mode": "job-local-proxy",
                "scope": config.scope,
                "grant_id": grant.get("grant_id"),
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        _unlink_quiet(ready_path)
    return 0


def configure(
    env: Mapping[str, str] | None = None,
    *,
    spawn: bool = True,
    wait_seconds: float = 15.0,
) -> int:
    """Print Turbo exports for a job-local proxy backed by short authority grants.

    Spawns a detached ``serve-state`` child so ``eval "$(... configure)"`` keeps a
    long-lived local proxy after the parent Python exits. Turbo continues using the
    same static TURBO_TOKEN while the child refreshes HMAC grants via CI_JOB_TOKEN.
    Child stdout/stderr go to a job-tmp log (masked from eval capture).
    """
    env = dict(os.environ if env is None else env)
    config = parse_config(env)
    if not config.enabled:
        print(f"turbo-broker: skipped ({config.denial or 'disabled'})", file=sys.stderr)
        return 0
    job_token = (env.get("CI_JOB_TOKEN") or "").strip()
    if not job_token:
        print("turbo-broker: CI_JOB_TOKEN required", file=sys.stderr)
        return 1
    state_root = _state_dir(env)
    local_token = secrets.token_urlsafe(32)
    ready_path = state_root / f"turbo-local-proxy-ready-{os.getpid()}.json"
    state_path = state_root / f"turbo-local-proxy-state-{os.getpid()}.json"
    child: subprocess.Popen[bytes] | None = None
    if ready_path.exists():
        _unlink_quiet(ready_path)
    state = {
        "broker_url": config.broker_url,
        "job_token": job_token,
        "scope": config.scope,
        "local_token": local_token,
        "ready_path": str(ready_path),
    }
    _write_private_json(state_path, state)
    try:
        if spawn:
            log_path = state_root / f"turbo-local-proxy-{os.getpid()}.log"
            # Mask child stdout/stderr from eval "$(configure)" — only exports on stdout.
            with open(log_path, "ab", buffering=0) as log_fh:
                os.fchmod(log_fh.fileno(), 0o600)
                child = subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "serve-state",
                        str(state_path),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=log_fh,
                    stderr=log_fh,
                    start_new_session=True,
                    close_fds=True,
                )
            deadline = time.time() + wait_seconds
            while time.time() < deadline:
                if child.poll() is not None:
                    print(
                        "turbo-broker: local proxy exited before ready",
                        file=sys.stderr,
                    )
                    return 1
                if ready_path.is_file() and ready_path.stat().st_size > 0:
                    break
                time.sleep(0.05)
            else:
                print(
                    "turbo-broker: local proxy failed to become ready",
                    file=sys.stderr,
                )
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        child.kill()
                return 1
            ready = json.loads(ready_path.read_text())
        else:
            # In-process mode for unit tests (daemon thread dies with process).
            session = GrantSession(
                broker_url=config.broker_url,
                job_token=job_token,
                scope=config.scope,
            )
            grant = session.current_grant()
            server = start_local_proxy(session=session, local_token=local_token)
            ready = {
                "turbo_api": f"http://{LOCAL_PROXY_HOST}:{server.server_port}",
                "turbo_token": local_token,
                "scope": config.scope,
                "grant_id": grant.get("grant_id"),
                "expires_at": grant.get("expires_at"),
                "mode": "job-local-proxy-inprocess",
            }
            _write_private_json(ready_path, ready)
    except (RuntimeError, ValueError, json.JSONDecodeError, OSError) as error:
        print(f"turbo-broker: {error}", file=sys.stderr)
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
        _unlink_quiet(state_path)
        _unlink_quiet(ready_path)
        return 1
    grant_stub = {
        "token": ready["turbo_token"],
        "team": DEFAULT_TEAM,
        "grant_id": ready.get("grant_id"),
        "expires_at": ready.get("expires_at"),
    }
    exports = export_env(
        grant_stub,
        config,
        turbo_api=str(ready["turbo_api"]),
        turbo_token=str(ready["turbo_token"]),
    )
    # stdout: shell exports only (eval-safe). Status metadata stays on stderr.
    sys.stdout.write(shell_exports(exports))
    print(
        json.dumps(
            {
                "scope": config.scope,
                "grant_id": ready.get("grant_id"),
                "expires_at": ready.get("expires_at"),
                "turbo_api": "job-local-proxy",
                "authority": "ci-cache-authority",
                "grant_ttl_refresh": "skew+401-reauthorize",
                "static_turbo_token": True,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "configure",
        help="Spawn job-local proxy + print static TURBO_* exports (eval-safe)",
    )
    sub.add_parser(
        "serve",
        help="Foreground alias: write state from env and serve (blocks)",
    )
    serve_state = sub.add_parser(
        "serve-state",
        help="Internal: serve from a state JSON path written by configure",
    )
    serve_state.add_argument("state_path")
    args = parser.parse_args(argv)
    if args.command == "configure":
        return configure()
    if args.command == "serve":
        # Foreground helper for operators: reuse configure spawn=False semantics
        # by building state from env and blocking in this process.
        env = dict(os.environ)
        config = parse_config(env)
        if not config.enabled:
            print(
                f"turbo-broker: skipped ({config.denial or 'disabled'})",
                file=sys.stderr,
            )
            return 0
        job_token = (env.get("CI_JOB_TOKEN") or "").strip()
        state_root = _state_dir(env)
        local_token = secrets.token_urlsafe(32)
        ready_path = state_root / f"turbo-local-proxy-ready-{os.getpid()}.json"
        state_path = state_root / f"turbo-local-proxy-state-{os.getpid()}.json"
        _write_private_json(
            state_path,
            {
                "broker_url": config.broker_url,
                "job_token": job_token,
                "scope": config.scope,
                "local_token": local_token,
                "ready_path": str(ready_path),
            },
        )
        # Print exports after ready by briefly forking readiness inline.
        session = GrantSession(
            broker_url=config.broker_url,
            job_token=job_token,
            scope=config.scope,
        )
        grant = session.current_grant()
        server = start_local_proxy(session=session, local_token=local_token)
        turbo_api = f"http://{LOCAL_PROXY_HOST}:{server.server_port}"
        exports = export_env(
            grant, config, turbo_api=turbo_api, turbo_token=local_token
        )
        sys.stdout.write(shell_exports(exports))
        sys.stdout.flush()
        print(
            json.dumps(
                {
                    "listening": turbo_api,
                    "mode": "job-local-proxy",
                    "scope": config.scope,
                    "grant_id": grant.get("grant_id"),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
            server.server_close()
        return 0
    if args.command == "serve-state":
        return serve_from_state(Path(args.state_path))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
