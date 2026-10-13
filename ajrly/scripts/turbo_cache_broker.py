#!/usr/bin/env python3
"""Request Turbo grants from ci-cache-authority after exact source checkout.

Ordinary jobs never receive the upstream Turbo writer credential. When the
broker is enabled, TURBO_API points at the authority proxy and TURBO_TOKEN is a
short-lived HMAC grant. Defaults keep the broker disabled until root canary.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse


DEFAULT_BROKER_URL = (
    "http://ci-cache-authority.gitlab-runners.svc.cluster.local:8090"
)
FORBIDDEN_UPSTREAM_HOSTS = (
    "turbo-cache-api.artifacts.svc",
    "turbo-cache.ajrly.com",
)


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


def request_grant(
    *,
    broker_url: str,
    job_token: str,
    scope: str,
    opener: urllib.request.OpenerDirector | None = None,
) -> dict:
    if not job_token.strip():
        raise ValueError("CI_JOB_TOKEN required")
    path = "/v1/authorize/write" if scope == "write" else "/v1/authorize/read"
    url = broker_url.rstrip("/") + path
    body = json.dumps({"audience": "turbo-cache-proxy"}).encode()
    req = urllib.request.Request(
        url,
        data=body,
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
        with open_fn(req, timeout=15) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:200]
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


def export_env(grant: dict, config: BrokerConfig) -> dict[str, str]:
    team = str(grant.get("team") or "ajrly")
    cache_mode = "local:rw,remote:rw" if config.scope == "write" else "local:rw,remote:r"
    return {
        "TURBO_API": config.broker_url,
        "TURBO_TOKEN": str(grant["token"]),
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
            # Avoid printing secrets when this helper is sourced incorrectly.
            lines.append(f"export {key}={json.dumps(value)}")
    return "\n".join(lines) + "\n"


def configure(env: Mapping[str, str] | None = None) -> int:
    env = dict(os.environ if env is None else env)
    config = parse_config(env)
    if not config.enabled:
        # Leave existing TURBO_* alone; do not invent remote tokens.
        print(f"turbo-broker: skipped ({config.denial or 'disabled'})", file=sys.stderr)
        return 0
    job_token = (env.get("CI_JOB_TOKEN") or "").strip()
    try:
        grant = request_grant(
            broker_url=config.broker_url,
            job_token=job_token,
            scope=config.scope,
        )
    except (RuntimeError, ValueError, json.JSONDecodeError, OSError) as error:
        print(f"turbo-broker: {error}", file=sys.stderr)
        return 1
    exports = export_env(grant, config)
    sys.stdout.write(shell_exports(exports))
    print(
        json.dumps(
            {
                "scope": config.scope,
                "grant_id": grant.get("grant_id"),
                "expires_at": grant.get("expires_at"),
                "turbo_api": "authority-proxy",
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("configure", help="Emit shell exports for Turbo broker grant")
    args = parser.parse_args(argv)
    if args.command == "configure":
        return configure()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
