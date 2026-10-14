#!/usr/bin/env python3
"""Tests for Turbo authority broker client wiring (templates)."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


class GitLabLoader(yaml.SafeLoader):
    pass


GitLabLoader.add_constructor(
    "!reference", lambda loader, node: loader.construct_sequence(node)
)


def load_mod():
    path = ROOT / "scripts" / "turbo_cache_broker.py"
    spec = importlib.util.spec_from_file_location("turbo_cache_broker", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Register before exec so @dataclass can resolve the module namespace (Py3.12+).
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeAuthority(ThreadingHTTPServer):
    """Minimal authority stand-in for local-proxy refresh tests."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.grants_issued = 0
        self.job_status = "running"
        self.tokens: list[str] = []
        self.authorize_bodies: list[dict] = []
        self.proxy_auths: list[str] = []
        self.clock = {"now": 1_700_000_000}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # noqa: A003
                return

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode() or "{}")
                outer.authorize_bodies.append(body)
                if outer.job_status != "running":
                    self._json(403, {"error": "job token not live/running"})
                    return
                if not (self.headers.get("JOB-TOKEN") or "").strip():
                    self._json(401, {"error": "JOB-TOKEN required"})
                    return
                outer.grants_issued += 1
                token = f"hmac-grant-{outer.grants_issued}"
                outer.tokens.append(token)
                grant = {
                    "grant_id": f"g{outer.grants_issued}",
                    "scope": "read",
                    "team": "ajrly",
                    "token": token,
                    "expires_at": int(outer.clock["now"]) + 600,
                }
                self._json(200, grant)

            def do_GET(self):  # noqa: N802
                self._proxy()

            def do_PUT(self):  # noqa: N802
                self._proxy()

            def _proxy(self):
                auth = self.headers.get("Authorization") or ""
                outer.proxy_auths.append(auth)
                if not auth.startswith("Bearer hmac-grant-"):
                    self._json(401, {"error": "grant expired"})
                    return
                token = auth.split(" ", 1)[1]
                # Simulate short TTL: only the latest grant is valid after clock skew.
                if outer.tokens and token != outer.tokens[-1]:
                    self._json(401, {"error": "grant expired"})
                    return
                # Expire by clock: grants minted when expires_at <= now are dead.
                # Token index encodes mint generation; expires_at tracked via issued count.
                issued = int(token.rsplit("-", 1)[-1])
                expires_at = (1_700_000_000 + (issued - 1) * 0) + 600
                # Use session skew relative to outer.clock for late requests.
                if int(outer.clock["now"]) >= expires_at and token == outer.tokens[0] and len(outer.tokens) == 1:
                    self._json(401, {"error": "grant expired"})
                    return
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(length) if length else b""
                if self.command == "PUT":
                    self.send_response(201)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload = body or b"cached"
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _json(self, status, payload):
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        super().__init__((host, port), Handler)
        self.daemon_threads = True


class TurboBrokerTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_mod()

    def test_broker_disabled_by_default(self):
        cfg = self.mod.parse_config({})
        self.assertFalse(cfg.enabled)
        self.assertFalse(cfg.write_enabled)
        self.assertEqual(cfg.denial, "broker_disabled")

    def test_write_disabled_even_when_broker_enabled(self):
        cfg = self.mod.parse_config({"AJRLY_TURBO_BROKER_ENABLED": "true"})
        self.assertTrue(cfg.enabled)
        self.assertFalse(cfg.write_enabled)
        self.assertEqual(cfg.scope, "read")

    def test_rejects_direct_upstream_writer_endpoint(self):
        cfg = self.mod.parse_config(
            {
                "AJRLY_TURBO_BROKER_ENABLED": "true",
                "AJRLY_TURBO_BROKER_URL": "http://turbo-cache-api.artifacts.svc:8080",
            }
        )
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.denial, "upstream_writer_endpoint_forbidden")

    def test_request_grant_uses_job_token_and_never_returns_upstream(self):
        grant = {
            "grant_id": "abc",
            "scope": "read",
            "team": "ajrly",
            "token": "hmac.grant",
            "expires_at": 1,
            "turbo_api": "proxy",
        }

        class FakeResp:
            def read(self):
                return json.dumps(grant).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class FakeOpener:
            def __init__(self):
                self.req = None

            def open(self, req, timeout=0):
                self.req = req
                return FakeResp()

        opener = FakeOpener()
        got = self.mod.request_grant(
            broker_url="http://ci-cache-authority.gitlab-runners.svc:8090",
            job_token="job-token",
            scope="read",
            opener=opener,
            body={
                "job_id": 999,
                "sha": "f" * 40,
                "AJRLY_SOURCE_VERIFIED": "true",
                "mode": "trusted",
            },
        )
        self.assertEqual(got["token"], "hmac.grant")
        headers = {k.lower(): v for k, v in opener.req.header_items()}
        self.assertEqual(headers.get("job-token"), "job-token")
        self.assertIn("/v1/authorize/read", opener.req.full_url)
        # Forged caller claims stripped from authorize body.
        sent = json.loads(opener.req.data.decode())
        self.assertEqual(sent, {"audience": "turbo-cache-proxy"})
        exports = self.mod.export_env(
            got,
            self.mod.BrokerConfig(
                enabled=True,
                write_enabled=False,
                broker_url="http://ci-cache-authority.gitlab-runners.svc:8090",
                scope="read",
            ),
        )
        self.assertEqual(
            exports["TURBO_API"],
            "http://ci-cache-authority.gitlab-runners.svc:8090",
        )
        self.assertEqual(exports["TURBO_TOKEN"], "hmac.grant")
        self.assertEqual(exports["TURBO_CACHE_READ_TOKEN"], "")
        self.assertNotIn("upstream", json.dumps(exports).lower())

    def test_grant_session_refreshes_on_clock_skew_not_caller_flags(self):
        clock = {"now": 1_700_000_000}
        issued = {"n": 0}

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload

            def read(self):
                return self._payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class FakeOpener:
            def open(self, req, timeout=0):
                issued["n"] += 1
                body = json.loads(req.data.decode())
                self.assertNotIn("sha", body)
                self.assertNotIn("job_id", body)
                grant = {
                    "grant_id": f"g{issued['n']}",
                    "token": f"tok-{issued['n']}",
                    "team": "ajrly",
                    "expires_at": int(clock["now"]) + 600,
                }
                return FakeResp(json.dumps(grant).encode())

        opener = FakeOpener()
        opener.assertNotIn = self.assertNotIn
        session = self.mod.GrantSession(
            broker_url="http://authority.test",
            job_token="live-job",
            scope="read",
            opener=opener,
            now=lambda: float(clock["now"]),
            skew_seconds=60,
        )
        first = session.current_grant()
        self.assertEqual(first["token"], "tok-1")
        self.assertEqual(issued["n"], 1)
        # Within TTL/skew: reuse.
        clock["now"] = 1_700_000_000 + 100
        self.assertEqual(session.current_grant()["token"], "tok-1")
        self.assertEqual(issued["n"], 1)
        # Near expiry: refresh with JOB-TOKEN only.
        clock["now"] = 1_700_000_000 + 560
        second = session.current_grant()
        self.assertEqual(second["token"], "tok-2")
        self.assertEqual(issued["n"], 2)

    def test_local_proxy_static_token_survives_authority_grant_refresh(self):
        authority = FakeAuthority()
        thread = threading.Thread(
            target=authority.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        self.addCleanup(authority.shutdown)
        self.addCleanup(authority.server_close)
        auth_url = f"http://127.0.0.1:{authority.server_address[1]}"
        clock = authority.clock
        session = self.mod.GrantSession(
            broker_url=auth_url,
            job_token="live-job",
            scope="read",
            now=lambda: float(clock["now"]),
            skew_seconds=60,
        )
        local_token = "static-turbo-token-for-real-client"
        proxy = self.mod.start_local_proxy(
            session=session, local_token=local_token, host="127.0.0.1", port=0
        )
        self.addCleanup(proxy.shutdown)
        self.addCleanup(proxy.server_close)
        turbo_api = f"http://127.0.0.1:{proxy.server_port}"

        def turbo_put():
            req = urllib.request.Request(
                turbo_api + "/v8/artifacts/demo",
                data=b"payload",
                method="PUT",
                headers={"Authorization": "Bearer " + local_token},
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                return resp.status

        self.assertEqual(turbo_put(), 201)
        self.assertEqual(authority.grants_issued, 1)
        # Advance past skew; same static Turbo token must still work via refresh.
        clock["now"] = 1_700_000_000 + 560
        self.assertEqual(turbo_put(), 201)
        self.assertGreaterEqual(authority.grants_issued, 2)
        # Completed job cannot refresh.
        authority.job_status = "success"
        clock["now"] = 1_700_000_000 + 1200
        session.invalidate()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            turbo_put()
        self.assertEqual(ctx.exception.code, 502)

    def test_local_proxy_rejects_caller_refresh_headers(self):
        authority = FakeAuthority()
        thread = threading.Thread(
            target=authority.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        self.addCleanup(authority.shutdown)
        self.addCleanup(authority.server_close)
        auth_url = f"http://127.0.0.1:{authority.server_address[1]}"
        session = self.mod.GrantSession(
            broker_url=auth_url,
            job_token="live-job",
            scope="read",
        )
        local_token = "static-token"
        proxy = self.mod.start_local_proxy(
            session=session, local_token=local_token, host="127.0.0.1", port=0
        )
        self.addCleanup(proxy.shutdown)
        self.addCleanup(proxy.server_close)
        req = urllib.request.Request(
            f"http://127.0.0.1:{proxy.server_port}/v8/artifacts/x",
            method="GET",
            headers={
                "Authorization": "Bearer " + local_token,
                "x-ci-cache-refresh": "1",
                "x-ci-cache-source-sha": "f" * 40,
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=2)
        self.assertEqual(ctx.exception.code, 403)

    def test_node_yml_defaults_and_order(self):
        text = (ROOT / "common" / "node.yml").read_text()
        node = yaml.load(text, Loader=GitLabLoader)
        variables = node[".node"]["variables"]
        self.assertEqual(variables.get("AJRLY_TURBO_BROKER_ENABLED"), "false")
        self.assertEqual(variables.get("AJRLY_TURBO_WRITE_ENABLED"), "false")
        self.assertIn(
            "ci-cache-authority.gitlab-runners.svc",
            variables.get("AJRLY_TURBO_BROKER_URL", ""),
        )
        # Exact checkout (.ajrly_source) precedes broker configure.
        self.assertIn("!reference [.ajrly_source, before_script]", text)
        self.assertIn("turbo_cache_broker.py", text)
        self.assertLess(
            text.index("!reference [.ajrly_source, before_script]"),
            text.index("turbo_cache_broker.py"),
        )
        self.assertLess(
            text.index("ajrly-dependency-cache.py restore"),
            text.index("turbo_cache_broker.py"),
        )
        # Must not hardcode upstream writer URL as TURBO_API default.
        self.assertNotIn("turbo-cache-api.artifacts", variables.get("TURBO_API", ""))
        self.assertNotIn("TURBO_CACHE_TOKEN", variables)

    def test_harbor_buildkit_uses_verified_digest_not_mutable_only(self):
        text = (ROOT / "images" / "harbor.yml").read_text()
        self.assertIn(
            "moby/buildkit:v0.33.1-rootless@sha256:"
            "f8a833b2de9d68e27f0815e4a737abdfaf8a2e4c615650557df11025101557b4",
            text,
        )
        # Do not invent alternate digests.
        self.assertNotIn("sha256:deadbeef", text)
        self.assertNotIn("sha256:000000", text)


if __name__ == "__main__":
    unittest.main()
