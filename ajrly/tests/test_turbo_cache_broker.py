#!/usr/bin/env python3
"""Tests for Turbo authority broker client wiring (templates)."""

from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import unittest
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
    spec.loader.exec_module(mod)
    return mod


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
        )
        self.assertEqual(got["token"], "hmac.grant")
        headers = {k.lower(): v for k, v in opener.req.header_items()}
        self.assertEqual(headers.get("job-token"), "job-token")
        self.assertIn("/v1/authorize/read", opener.req.full_url)
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
