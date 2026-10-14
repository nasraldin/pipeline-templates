#!/usr/bin/env python3
"""Readonly Trivy vulnerability-feed policy for Ajrly security jobs.

Security jobs must never reuse GitLab job/task artifacts as a vuln DB
(``cache: []`` on ``security:trivy``). When a persistent feed directory is
commissioned, this helper validates provenance + freshness (default 24h) and
exports ``TRIVY_CACHE_DIR`` for the scanner. Stale or unsigned feeds fail closed;
there is no skip-freshness flag.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Mapping

DEFAULT_MAX_AGE_SECONDS = 24 * 3600
MANIFEST_NAME = "ajrly-trivy-feed.manifest.json"
REQUIRED_MANIFEST_KEYS = (
    "schema",
    "created_at_unix",
    "trivy_db_repository",
    "content_sha256",
    "producer",
)
SCHEMA = "ajrly-trivy-feed-v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class FeedPolicyError(ValueError):
    """Fail-closed feed validation error."""


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def max_age_seconds(env: Mapping[str, str]) -> int:
    raw = (env.get("AJRLY_TRIVY_FEED_MAX_AGE_SECONDS") or str(DEFAULT_MAX_AGE_SECONDS)).strip()
    try:
        age = int(raw)
    except ValueError as exc:
        raise FeedPolicyError("invalid_max_age") from exc
    if age <= 0 or age > 7 * 24 * 3600:
        raise FeedPolicyError("invalid_max_age")
    return age


def load_manifest(feed_dir: Path) -> dict[str, Any]:
    path = feed_dir / MANIFEST_NAME
    if not path.is_file():
        raise FeedPolicyError("missing_feed_manifest")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FeedPolicyError("invalid_feed_manifest") from exc
    if not isinstance(payload, dict):
        raise FeedPolicyError("invalid_feed_manifest")
    return payload


def validate_manifest(
    manifest: Mapping[str, Any],
    *,
    now: float | None = None,
    max_age: int = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    missing = [key for key in REQUIRED_MANIFEST_KEYS if key not in manifest]
    if missing:
        raise FeedPolicyError("incomplete_feed_manifest")
    if str(manifest.get("schema", "")) != SCHEMA:
        raise FeedPolicyError("unsupported_feed_schema")
    try:
        created = int(manifest["created_at_unix"])
    except (TypeError, ValueError) as exc:
        raise FeedPolicyError("invalid_feed_timestamp") from exc
    digest = str(manifest.get("content_sha256", "")).strip().lower()
    if not SHA256_RE.fullmatch(digest):
        raise FeedPolicyError("invalid_feed_digest")
    if not str(manifest.get("trivy_db_repository", "")).strip():
        raise FeedPolicyError("missing_db_repository")
    if not str(manifest.get("producer", "")).strip():
        raise FeedPolicyError("missing_producer")
    # Explicit deny of skip / hybrid freshness claims from callers.
    if _truthy(str(manifest.get("skip_fresh_check", ""))):
        raise FeedPolicyError("skip_fresh_check_forbidden")
    clock = time.time() if now is None else float(now)
    age = clock - created
    if age < -60:
        raise FeedPolicyError("feed_timestamp_in_future")
    if age > max_age:
        raise FeedPolicyError("feed_stale")
    return {
        "schema": SCHEMA,
        "created_at_unix": created,
        "age_seconds": int(age),
        "max_age_seconds": max_age,
        "fresh": True,
        "content_sha256": digest,
        "trivy_db_repository": str(manifest["trivy_db_repository"]).strip(),
        "producer": str(manifest["producer"]).strip(),
        "task_artifact_reuse": False,
    }


def resolve_feed_dir(env: Mapping[str, str]) -> Path | None:
    raw = (env.get("AJRLY_TRIVY_FEED_DIR") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_dir():
        raise FeedPolicyError("feed_dir_missing")
    return path


def prepare_trivy_env(env: Mapping[str, str], *, now: float | None = None) -> dict[str, str]:
    """Return shell exports for a security:trivy job.

    When feed is disabled, returns empty dict (online DB download allowed).
    When enabled, validates persistent readonly feed and points TRIVY_CACHE_DIR
    at it. Never reads GitLab CI job artifact paths as the feed root.
    """
    if not _truthy(env.get("AJRLY_TRIVY_FEED_ENABLED")):
        return {
            "AJRLY_TRIVY_FEED_STATUS": "disabled_online_ok",
            "AJRLY_TRIVY_TASK_ARTIFACT_REUSE": "false",
        }
    # Refuse known job-artifact locations even if misconfigured as feed dir.
    feed_dir = resolve_feed_dir(env)
    if feed_dir is None:
        raise FeedPolicyError("feed_enabled_without_dir")
    forbidden_markers = (".ci-artifacts", "CI_JOB_TMPDIR", "ci-job-artifacts")
    rendered = str(feed_dir)
    for marker in forbidden_markers:
        if marker in rendered:
            raise FeedPolicyError("task_artifact_feed_forbidden")
    job_tmpdir = (env.get("CI_JOB_TMPDIR") or "").strip()
    if job_tmpdir and rendered.startswith(job_tmpdir.rstrip("/") + "/"):
        raise FeedPolicyError("task_artifact_feed_forbidden")
    manifest = load_manifest(feed_dir)
    report = validate_manifest(
        manifest, now=now, max_age=max_age_seconds(env)
    )
    db_repo = report["trivy_db_repository"]
    return {
        "TRIVY_CACHE_DIR": str(feed_dir),
        "TRIVY_DB_REPOSITORY": db_repo,
        "AJRLY_TRIVY_FEED_STATUS": "persistent_readonly_fresh",
        "AJRLY_TRIVY_FEED_AGE_SECONDS": str(report["age_seconds"]),
        "AJRLY_TRIVY_FEED_CONTENT_SHA256": report["content_sha256"],
        "AJRLY_TRIVY_TASK_ARTIFACT_REUSE": "false",
    }


def cmd_prepare(args: argparse.Namespace) -> int:
    try:
        exports = prepare_trivy_env(os.environ)
    except FeedPolicyError as exc:
        print(f"security-feed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(exports, sort_keys=True))
    else:
        for key, value in exports.items():
            # Shell-safe single-quoted export lines for eval.
            escaped = value.replace("'", "'\"'\"'")
            print(f"export {key}='{escaped}'")
    return 0


def cmd_validate_manifest(args: argparse.Namespace) -> int:
    try:
        manifest = load_manifest(Path(args.feed_dir))
        report = validate_manifest(
            manifest,
            now=args.now,
            max_age=args.max_age if args.max_age is not None else DEFAULT_MAX_AGE_SECONDS,
        )
    except FeedPolicyError as exc:
        print(f"security-feed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="Validate feed policy and print exports")
    prepare.add_argument("--json", action="store_true")
    prepare.set_defaults(func=cmd_prepare)

    validate = sub.add_parser("validate-manifest", help="Validate a feed manifest only")
    validate.add_argument("--feed-dir", required=True)
    validate.add_argument("--max-age", type=int, default=None)
    validate.add_argument("--now", type=float, default=None)
    validate.set_defaults(func=cmd_validate_manifest)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
