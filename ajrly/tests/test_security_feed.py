#!/usr/bin/env python3
"""Policy/timestamp tests for persistent Trivy feed helper."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "security_feed.py"


def load_mod():
    spec = importlib.util.spec_from_file_location("ajrly_security_feed", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class SecurityFeedTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_mod()

    def _manifest(self, **overrides):
        base = {
            "schema": self.mod.SCHEMA,
            "created_at_unix": int(time.time()) - 60,
            "trivy_db_repository": "ghcr.io/aquasecurity/trivy-db:2",
            "content_sha256": "a" * 64,
            "producer": "root-maintenance-only",
        }
        base.update(overrides)
        return base

    def test_fresh_manifest_passes(self):
        report = self.mod.validate_manifest(self._manifest(), max_age=86400)
        self.assertTrue(report["fresh"])
        self.assertFalse(report["task_artifact_reuse"])
        self.assertEqual(report["max_age_seconds"], 86400)

    def test_stale_manifest_fails_closed(self):
        stale = self._manifest(created_at_unix=int(time.time()) - 90000)
        with self.assertRaises(self.mod.FeedPolicyError) as ctx:
            self.mod.validate_manifest(stale, max_age=86400)
        self.assertEqual(str(ctx.exception), "feed_stale")

    def test_skip_fresh_check_forbidden(self):
        bad = self._manifest(skip_fresh_check=True)
        with self.assertRaises(self.mod.FeedPolicyError) as ctx:
            self.mod.validate_manifest(bad)
        self.assertEqual(str(ctx.exception), "skip_fresh_check_forbidden")

    def test_prepare_disabled_allows_online(self):
        exports = self.mod.prepare_trivy_env({"AJRLY_TRIVY_FEED_ENABLED": "false"})
        self.assertEqual(exports["AJRLY_TRIVY_FEED_STATUS"], "disabled_online_ok")
        self.assertEqual(exports["AJRLY_TRIVY_TASK_ARTIFACT_REUSE"], "false")
        self.assertNotIn("TRIVY_CACHE_DIR", exports)

    def test_prepare_enabled_requires_dir(self):
        with self.assertRaises(self.mod.FeedPolicyError) as ctx:
            self.mod.prepare_trivy_env({"AJRLY_TRIVY_FEED_ENABLED": "true"})
        self.assertEqual(str(ctx.exception), "feed_enabled_without_dir")

    def test_prepare_rejects_task_artifact_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            feed = Path(tmp) / ".ci-artifacts" / "trivy"
            feed.mkdir(parents=True)
            (feed / self.mod.MANIFEST_NAME).write_text(
                json.dumps(self._manifest()), encoding="utf-8"
            )
            with self.assertRaises(self.mod.FeedPolicyError) as ctx:
                self.mod.prepare_trivy_env(
                    {
                        "AJRLY_TRIVY_FEED_ENABLED": "true",
                        "AJRLY_TRIVY_FEED_DIR": str(feed),
                    }
                )
            self.assertEqual(str(ctx.exception), "task_artifact_feed_forbidden")

    def test_prepare_persistent_readonly_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            feed = Path(tmp) / "persistent-trivy-feed"
            feed.mkdir()
            (feed / self.mod.MANIFEST_NAME).write_text(
                json.dumps(self._manifest()), encoding="utf-8"
            )
            exports = self.mod.prepare_trivy_env(
                {
                    "AJRLY_TRIVY_FEED_ENABLED": "true",
                    "AJRLY_TRIVY_FEED_DIR": str(feed),
                    "AJRLY_TRIVY_FEED_MAX_AGE_SECONDS": "86400",
                }
            )
            self.assertEqual(exports["TRIVY_CACHE_DIR"], str(feed))
            self.assertEqual(exports["AJRLY_TRIVY_FEED_STATUS"], "persistent_readonly_fresh")
            self.assertEqual(exports["AJRLY_TRIVY_TASK_ARTIFACT_REUSE"], "false")
            self.assertEqual(exports["AJRLY_TRIVY_FEED_CONTENT_SHA256"], "a" * 64)

    def test_security_trivy_declares_empty_cache(self):
        text = (ROOT / "security" / "monorepo.yml").read_text(encoding="utf-8")
        self.assertIn("security:trivy:", text)
        # Between security:trivy and next job / EOF, cache must be empty list.
        block = text.split("security:trivy:", 1)[1]
        self.assertIn("cache: []", block.split("security:", 1)[0] if "security:" in block[1:] else block)
        self.assertIn("security_feed.py", text)


if __name__ == "__main__":
    unittest.main()
