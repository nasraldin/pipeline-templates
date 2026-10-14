"""Failing-first tests for verified pnpm-store dependency archive restore."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import pathlib
import re
import shutil
import sys
import tarfile
import tempfile
import time
import unittest
import urllib.error
from typing import Dict, Optional
from unittest import mock

import yaml

SCRIPT = pathlib.Path(__file__).parents[1] / "scripts/dependency_cache.py"
NODE_YML = pathlib.Path(__file__).parents[1] / "common/node.yml"
CANARY_YML = pathlib.Path(__file__).parents[1] / "canary/node-cache-consumer.yml"
PIPELINE_YML = pathlib.Path(__file__).parents[1] / "pipeline.yml"
ROOT = pathlib.Path(__file__).parents[1]

# Verified Harbor digest from platform/ci-tools/versions.json + registry-digest.json
# (ci-tools-registry-publication RESULT). Must end with becee899 — never invent.
VERIFIED_NODE_DIGEST = (
    "sha256:7a5a6894d32a279390b9c27a16feee922297a0e942b3c6e3900e11b0becee899"
)


class GitLabLoader(yaml.SafeLoader):
    pass


GitLabLoader.add_constructor("!reference", lambda loader, node: loader.construct_sequence(node))


class MemoryS3:
    """In-memory S3Reader/S3Writer for unit tests."""

    def __init__(self, objects: Optional[Dict[str, bytes]] = None):
        self.objects = dict(objects or {})
        self.puts = []

    def fetch_bytes(self, object_key: str) -> bytes:
        if object_key not in self.objects:
            raise FileNotFoundError(object_key)
        return self.objects[object_key]

    def fetch(self, object_key: str, destination: pathlib.Path) -> int:
        data = self.fetch_bytes(object_key)
        destination.write_bytes(data)
        return len(data)

    def put(self, object_key: str, data: bytes, *, content_type: str = "") -> None:
        self.puts.append((object_key, len(data), content_type))
        self.objects[object_key] = data


class DependencyCacheTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.exists(), "dependency_cache.py must exist")
        spec = importlib.util.spec_from_file_location("ajrly_dependency_cache", SCRIPT)
        self.mod = importlib.util.module_from_spec(spec)
        # Register before exec so @dataclass can resolve the module namespace (Py3.12+).
        sys.modules[spec.name] = self.mod
        spec.loader.exec_module(self.mod)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.toolchain = {"node": "26.11.1", "pnpm": "12.10.1"}
        self._write_identity(
            lock="lock-v1\n",
            workspace="packages:\n  - apps/*\n",
            patches={"braces@patch": b"diff-a\n"},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _write_identity(self, *, lock: str, workspace: str, patches: Dict[str, bytes]):
        (self.root / "pnpm-lock.yaml").write_text(lock)
        (self.root / "pnpm-workspace.yaml").write_text(workspace)
        patch_dir = self.root / "patches"
        if patch_dir.exists():
            shutil.rmtree(patch_dir)
        patch_dir.mkdir()
        for name, body in sorted(patches.items()):
            (patch_dir / name).write_bytes(body)
        (self.root / "apps").mkdir(exist_ok=True)
        (self.root / "apps" / "readme.txt").write_text("source\n")
        (self.root / ".ajrly-source.json").write_text(
            json.dumps({"repository": "nasraldin/ajrly-platform", "sha": "a" * 40}) + "\n"
        )

    def _key(self, scope: str = "full") -> str:
        return self.mod.dependency_key(self.root, scope, self.toolchain)

    def _store_bytes(self, files: Dict[str, bytes]) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, data in sorted(files.items()):
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                info.mtime = 1_700_000_000
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))
        return buf.getvalue()

    def _manifest(self, key: str, archive: bytes, *, expanded: int, created_at: Optional[float] = None,
                  scope: str = "full", corrupt_sha: bool = False) -> dict:
        return {
            "schema": 1,
            "key": key,
            "toolchain": dict(self.toolchain),
            "scope": scope,
            "created_at": created_at if created_at is not None else time.time(),
            "archive_sha256": ("0" * 64) if corrupt_sha else hashlib.sha256(archive).hexdigest(),
            "compressed_bytes": len(archive),
            "expanded_bytes": expanded,
        }

    def _publish_local(self, files: Dict[str, bytes], *, scope: str = "full") -> tuple[str, MemoryS3]:
        store = self.root / ".pnpm-store"
        if store.exists():
            shutil.rmtree(store)
        store.mkdir()
        for name, data in files.items():
            path = store / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        key = self._key(scope)
        writer = MemoryS3()
        result = self.mod.publish_store(self.root, key, writer, scope=scope, toolchain=self.toolchain)
        self.assertEqual(result["key"], key)
        return key, writer

    def test_key_changes_for_lock_workspace_patch_toolchain_libc_not_source(self):
        base = self._key()
        (self.root / "apps" / "readme.txt").write_text("unrelated source edit\n")
        self.assertEqual(base, self._key(), "unrelated source edits must not change the key")

        self._write_identity(lock="lock-v2\n", workspace="packages:\n  - apps/*\n",
                             patches={"braces@patch": b"diff-a\n"})
        self.assertNotEqual(base, self._key(), "lockfile edits must invalidate")

        self._write_identity(lock="lock-v1\n", workspace="packages:\n  - packages/*\n",
                             patches={"braces@patch": b"diff-a\n"})
        self.assertNotEqual(base, self._key(), "workspace edits must invalidate")

        self._write_identity(lock="lock-v1\n", workspace="packages:\n  - apps/*\n",
                             patches={"braces@patch": b"diff-b\n"})
        self.assertNotEqual(base, self._key(), "patch edits must invalidate")

        other = self.mod.dependency_key(self.root, "full", {"node": "26.11.1", "pnpm": "12.10.2"})
        self.assertNotEqual(base, other, "toolchain edits must invalidate")

        with mock.patch.object(self.mod, "detect_libc", side_effect=["gnu", "musl"]):
            gnu_key = self.mod.dependency_key(self.root, "full", self.toolchain)
            musl_key = self.mod.dependency_key(self.root, "full", self.toolchain)
        self.assertNotEqual(gnu_key, musl_key, "libc edits must invalidate")

        with mock.patch.object(
            self.mod, "detect_platform", side_effect=["linux-x86_64", "linux-aarch64"]
        ):
            amd_key = self.mod.dependency_key(self.root, "full", self.toolchain)
            arm_key = self.mod.dependency_key(self.root, "full", self.toolchain)
        self.assertNotEqual(amd_key, arm_key, "arch/platform edits must invalidate")

        scope_key = self.mod.dependency_key(self.root, "apps/web/admin", self.toolchain)
        self.assertNotEqual(base, scope_key)
        self.assertTrue(base.startswith("dep-v1-"))
        self.assertRegex(base, r"^dep-v1-[0-9a-f]{64}$")

    def test_restore_report_separates_configured_vs_observed_registry(self):
        """Caller --upstream-host / pnpm config is configured_registry, not traffic proof."""
        result = self.mod.RestoreResult("hit", 4096, 1.25, "")
        # Config-only path: no observed hosts → must not claim npm_only / observed traffic.
        report = self.mod.format_restore_report(
            result,
            key="dep-v1-" + ("a" * 64),
            phase="warm",
            install_elapsed_s=3.5,
            configured_registry="https://npm.ajrly.com/",
            observed_registry_hosts=[],
        )
        self.assertEqual(report["status"], "hit")
        self.assertEqual(report["phase"], "warm")
        self.assertEqual(report["bytes"], 4096)
        self.assertEqual(report["install_elapsed_s"], 3.5)
        self.assertEqual(report["configured_registry"], "https://npm.ajrly.com/")
        self.assertEqual(report["observed_registry_hosts"], [])
        self.assertNotIn("npm_only", report)
        self.assertNotIn("upstream_hosts", report)
        self.assertEqual(report["max_compressed_bytes"], 2 * 1024 ** 3)
        self.assertEqual(report["acceptance_class"], "persistent_canonical_restore")
        self.assertFalse(report["local_store_warm"])
        blob = json.dumps(report)
        self.assertNotIn("AKIA", blob)
        self.assertNotIn("secret", blob.lower())
        self.assertNotIn("X-Amz", blob)

        # Observed hosts only set npm_only from real network/source evidence.
        observed = self.mod.format_restore_report(
            result,
            configured_registry="https://npm.ajrly.com/",
            observed_registry_hosts=["npm.ajrly.com", "npm.ajrly.com"],
        )
        self.assertEqual(observed["observed_registry_hosts"], ["npm.ajrly.com"])
        self.assertTrue(observed["npm_only"])
        mixed = self.mod.format_restore_report(
            result,
            configured_registry="https://npm.ajrly.com/",
            observed_registry_hosts=["npm.ajrly.com", "registry.npmjs.org"],
        )
        self.assertFalse(mixed["npm_only"])
        # Legacy caller upstream_hosts alone must not invent npm_only success.
        legacy = self.mod.format_restore_report(
            result, upstream_hosts=["npm.ajrly.com"]
        )
        self.assertEqual(legacy.get("configured_registry", ""), "")
        self.assertEqual(legacy["observed_registry_hosts"], [])
        self.assertNotIn("npm_only", legacy)

        miss = self.mod.format_restore_report(
            self.mod.RestoreResult("miss", 0, 0.1, "not_found"),
            phase="cold",
            local_store_warm=False,
        )
        self.assertEqual(miss["acceptance_class"], "miss_no_fabricated_hit")
        local_warm = self.mod.format_restore_report(
            self.mod.RestoreResult("miss", 0, 0.1, "not_found"),
            phase="cold",
            local_store_warm=True,
            store_bytes=1024,
            pnpm_progress={"resolved": 12, "reused": 0, "downloaded": 12, "added": 12},
        )
        self.assertEqual(local_warm["acceptance_class"], "local_store_warm_not_persistent")
        self.assertEqual(local_warm["store_bytes"], 1024)
        self.assertEqual(local_warm["pnpm_progress"]["downloaded"], 12)
        self.assertNotIn("npm_request_estimate", local_warm)
        self.assertNotEqual(
            local_warm["acceptance_class"], "persistent_canonical_restore",
            "same-job local store warm must never claim persistent restore acceptance",
        )
        # Zero package-count heuristic must not fabricate upstream request proof.
        zero_est = self.mod.format_restore_report(
            result, npm_request_estimate=0, store_bytes=2048,
        )
        self.assertNotIn("npm_request_estimate", zero_est)

    def test_immutable_templates_ref_uses_server_file_metadata(self):
        """Script fetch must pin immutable SHA; server metadata is authority not caller trust."""
        branch = "codex/ci-cache-templates"
        with self.assertRaises(ValueError) as raised:
            self.mod.require_immutable_templates_ref(branch)
        self.assertIn("templates_ref_not_immutable", str(raised.exception))

        pin = "b318ecdf953767b2b7ac171b944a7646c37b250d"
        self.assertEqual(self.mod.require_immutable_templates_ref(pin), pin)

        url = self.mod.templates_script_raw_url(
            "http://192.168.68.12/api/v4/projects/1", pin, "dependency_cache.py"
        )
        self.assertIn(f"ref={pin}", url)
        with self.assertRaises(ValueError):
            self.mod.templates_script_raw_url(
                "http://192.168.68.12/api/v4/projects/1", branch, "dependency_cache.py"
            )

        server_meta = {
            "file_name": "dependency_cache.py",
            "ref": pin,
            "commit_id": pin,
            "last_commit_id": "6d2bf3a5317b89125ddaa56ca65b1fb77f70e82c",
            "content_sha256": "bcdb6c4aa9e6e5382fcb7732a63b93fc06883a2fb85cd8e008ac42859e264d28",
            "size": 35410,
        }
        # Caller-written trust fields must not override server metadata.
        verified = self.mod.verify_templates_file_metadata(
            server_meta,
            expected_ref=pin,
            caller_trust={"commit_id": "f" * 40, "content_sha256": "0" * 64},
        )
        self.assertEqual(verified["commit_id"], pin)
        self.assertEqual(
            verified["content_sha256"],
            "bcdb6c4aa9e6e5382fcb7732a63b93fc06883a2fb85cd8e008ac42859e264d28",
        )
        with self.assertRaises(ValueError) as mismatch:
            self.mod.verify_templates_file_metadata(
                {**server_meta, "commit_id": "a" * 40},
                expected_ref=pin,
            )
        self.assertIn("templates_ref_metadata_mismatch", str(mismatch.exception))

    def test_transport_bound_pin_records_local_hash_not_server_metadata_authority(self):
        """/raw pin is transport-bound; never label local digest as server metadata proof."""
        pin = "b318ecdf953767b2b7ac171b944a7646c37b250d"
        path = pathlib.Path(tempfile.mkdtemp(prefix="pin-raw-")) / "dependency_cache.py"
        self.addCleanup(lambda: shutil.rmtree(path.parent, ignore_errors=True))
        body = b"#!/usr/bin/env python3\n# pinned helper body\n"
        path.write_bytes(body)

        record = self.mod.record_transport_bound_pin(path, pin)
        self.assertEqual(record["ref"], pin)
        self.assertEqual(record["verify"], "transport_bound_pin")
        self.assertEqual(record["authority"], "immutable_git_ref_via_raw")
        self.assertIs(record["server_metadata_authority"], False)
        self.assertEqual(record["content_sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(record["bytes"], len(body))
        self.assertFalse(record["response_headers_commit_bound"])

        bound = self.mod.record_transport_bound_pin(
            path, pin, response_headers={"X-Gitlab-Commit-Id": pin}
        )
        self.assertEqual(bound["response_commit_id"], pin)
        self.assertTrue(bound["response_headers_commit_bound"])
        self.assertIs(bound["server_metadata_authority"], False)

        with self.assertRaises(ValueError) as mismatch:
            self.mod.record_transport_bound_pin(
                path, pin, response_headers={"X-Gitlab-Commit-Id": "a" * 40}
            )
        self.assertIn("templates_raw_commit_header_mismatch", str(mismatch.exception))

        empty = path.parent / "empty.py"
        empty.write_bytes(b"")
        with self.assertRaises(ValueError) as empty_err:
            self.mod.record_transport_bound_pin(empty, pin)
        self.assertIn("templates_raw_empty_or_missing", str(empty_err.exception))

        with self.assertRaises(ValueError) as mutable:
            self.mod.record_transport_bound_pin(path, "codex/ci-cache-templates")
        self.assertIn("templates_ref_not_immutable", str(mutable.exception))
        with self.assertRaises(ValueError):
            self.mod.record_transport_bound_pin(path, pin[:12])

    def test_fetch_pinned_raw_uses_job_token_and_skips_duplicate_recheck_by_default(self):
        pin = "b318ecdf953767b2b7ac171b944a7646c37b250d"
        body = b"#!/usr/bin/env python3\nprint('ok')\n"
        calls = []

        class _Resp:
            def __init__(self, payload: bytes):
                self._payload = payload
                self.headers = {"X-Gitlab-Commit-Id": pin}

            def read(self):
                return self._payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def opener(request, timeout=30):
            calls.append(request)
            self.assertEqual(request.get_header("Job-token"), "ci-job-token-fixture")
            self.assertIn("/raw?", request.full_url)
            self.assertIn(f"ref={pin}", request.full_url)
            return _Resp(body)

        dest = pathlib.Path(tempfile.mkdtemp(prefix="fetch-raw-")) / "dependency_cache.py"
        self.addCleanup(lambda: shutil.rmtree(dest.parent, ignore_errors=True))
        record = self.mod.fetch_templates_script_raw(
            "http://192.168.68.12/api/v4/projects/1",
            pin,
            "dependency_cache.py",
            job_token="ci-job-token-fixture",
            destination=dest,
            opener=opener,
        )
        self.assertEqual(len(calls), 1)
        self.assertFalse(record["recheck"])
        self.assertEqual(record["verify"], "transport_bound_pin")
        self.assertIs(record["server_metadata_authority"], False)
        self.assertEqual(dest.read_bytes(), body)

        record2 = self.mod.fetch_templates_script_raw(
            "http://192.168.68.12/api/v4/projects/1",
            pin,
            "dependency_cache.py",
            job_token="ci-job-token-fixture",
            destination=dest,
            opener=opener,
            recheck=True,
        )
        self.assertEqual(len(calls), 3)  # 1 prior + 2 for recheck path
        self.assertTrue(record2["recheck"])

        def empty_opener(request, timeout=30):
            return _Resp(b"")

        with self.assertRaises(ValueError) as empty_err:
            self.mod.fetch_templates_script_raw(
                "http://192.168.68.12/api/v4/projects/1",
                pin,
                job_token="ci-job-token-fixture",
                destination=dest,
                opener=empty_opener,
            )
        self.assertIn("templates_raw_empty_or_missing", str(empty_err.exception))

    def test_file_metadata_404_fixture_fails_closed_without_admin_token(self):
        """Metadata 404 under JOB-TOKEN is real policy; fail closed, no token broaden."""
        pin = "b318ecdf953767b2b7ac171b944a7646c37b250d"
        seen = {}

        def opener(request, timeout=30):
            seen["url"] = request.full_url
            seen["job_token"] = request.get_header("Job-token")
            raise urllib.error.HTTPError(
                request.full_url, 404, "Not Found", hdrs=None, fp=io.BytesIO(b"")
            )

        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.mod.fetch_templates_file_metadata(
                "http://192.168.68.12/api/v4/projects/1",
                pin,
                "dependency_cache.py",
                job_token="ci-job-token-fixture",
                opener=opener,
            )
        self.assertEqual(raised.exception.code, 404)
        self.assertEqual(seen["job_token"], "ci-job-token-fixture")
        self.assertNotIn("/raw?", seen["url"])
        # Truncated / mutable refs still rejected before network.
        with self.assertRaises(ValueError):
            self.mod.fetch_templates_file_metadata(
                "http://192.168.68.12/api/v4/projects/1",
                pin[:7],
                job_token="ci-job-token-fixture",
                opener=opener,
            )

    def test_closure_timing_ratio_report_persists_cold_warm_variance(self):
        """Persist cold-miss + warm restore/install timings; no whole-pipeline 25% claim."""
        phases = [
            {"phase": "cold", "status": "miss", "install_elapsed_s": 545.0,
             "restore_duration_s": 0.047, "pnpm_progress": {
                 "resolved": 2778, "reused": 0, "downloaded": 2611, "added": 2748}},
            {"phase": "warm", "status": "hit", "install_elapsed_s": 219.0,
             "restore_duration_s": 78.2, "pnpm_progress": {
                 "resolved": 2778, "reused": 2778, "downloaded": 0, "added": 2748}},
            {"phase": "warm2", "status": "hit", "install_elapsed_s": 225.0,
             "restore_duration_s": 74.5, "pnpm_progress": {
                 "resolved": 2778, "reused": 2778, "downloaded": 0, "added": 2748}},
        ]
        summary = self.mod.closure_timing_ratio_report(phases)
        self.assertEqual(summary["cold_install_s"], 545.0)
        self.assertAlmostEqual(summary["warm_install_mean_s"], 222.0)
        self.assertAlmostEqual(summary["warm_restore_mean_s"], 76.35, places=2)
        # Wall = restore + install for warm vs cold install-only baseline.
        self.assertIn("warm_total_mean_s", summary)
        self.assertIn("install_wall_reduction_ratio", summary)
        self.assertLess(summary["install_wall_reduction_ratio"], 0.55)
        self.assertGreater(summary["install_wall_reduction_ratio"], 0.35)
        self.assertFalse(summary["whole_pipeline_25pct_claim"])
        self.assertEqual(summary["objective_target_status"], "pending")
        self.assertIn("variance", summary)

    def test_restore_requires_verified_source_manifest(self):
        key, reader = self._publish_local({"index/v1": b"pkg\n"})
        (self.root / ".ajrly-source.json").unlink()
        shutil.rmtree(self.root / ".pnpm-store")
        result = self.mod.restore_store(self.root, key, reader, scope="full", toolchain=self.toolchain)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.reason, "missing_source_verification")
        self.assertFalse((self.root / ".pnpm-store").exists())

    def test_caller_selected_key_is_rejected_and_staging_removed(self):
        key, reader = self._publish_local({"index/v1": b"pkg\n"})
        shutil.rmtree(self.root / ".pnpm-store")
        forged = "dep-v1-" + ("f" * 64)
        staging_parent = self.root / ".ci-tmp" / "dependency-cache"
        staging_parent.mkdir(parents=True)
        result = self.mod.restore_store(
            self.root, forged, reader, scope="full", toolchain=self.toolchain, staging_dir=staging_parent
        )
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.reason, "caller_selected_key")
        self.assertFalse((self.root / ".pnpm-store").exists())
        self.assertEqual(list(staging_parent.iterdir()), [])

    def test_restore_hit_promotes_atomically(self):
        key, reader = self._publish_local({"files/a": b"alpha", "files/b": b"beta"})
        shutil.rmtree(self.root / ".pnpm-store")
        result = self.mod.restore_store(self.root, key, reader, scope="full", toolchain=self.toolchain)
        self.assertEqual(result.status, "hit")
        self.assertGreater(result.bytes, 0)
        self.assertGreaterEqual(result.duration, 0.0)
        self.assertEqual((self.root / ".pnpm-store" / "files" / "a").read_bytes(), b"alpha")
        self.assertEqual((self.root / ".pnpm-store" / "files" / "b").read_bytes(), b"beta")

    def test_reject_path_traversal_symlink_device_and_leave_canonical_untouched(self):
        key = self._key()
        payload = b"nope"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            evil = tarfile.TarInfo(name="../escape.txt")
            evil.size = len(payload)
            tar.addfile(evil, io.BytesIO(payload))
        archive = buf.getvalue()
        reader = MemoryS3({
            self.mod.manifest_object_key(key): json.dumps(
                self._manifest(key, archive, expanded=len(payload))
            ).encode(),
            self.mod.archive_object_key(key): archive,
        })
        before = dict(reader.objects)
        result = self.mod.restore_store(self.root, key, reader, scope="full", toolchain=self.toolchain)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.reason, "unsafe_archive_member")
        self.assertEqual(reader.objects, before)
        self.assertFalse((self.root / "escape.txt").exists())
        self.assertFalse((self.root / ".pnpm-store").exists())

    def test_reject_hardlink_prefix_bypass_and_zero_expanded_tar_bomb(self):
        key = self._key()
        # Hardlink target path that shares a string prefix with the extract root.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            body = b"ok"
            info = tarfile.TarInfo(name="ok")
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
            link = tarfile.TarInfo(name="evil-link")
            link.type = tarfile.LNKTYPE
            # Absolute-looking relative escape rejected by safe path; use .. form.
            link.linkname = "../escape-target"
            tar.addfile(link)
        archive = buf.getvalue()
        reader = MemoryS3({
            self.mod.manifest_object_key(key): json.dumps(
                self._manifest(key, archive, expanded=len(b"ok"))
            ).encode(),
            self.mod.archive_object_key(key): archive,
        })
        before = dict(reader.objects)
        result = self.mod.restore_store(self.root, key, reader, scope="full", toolchain=self.toolchain)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.reason, "unsafe_archive_member")
        self.assertEqual(reader.objects, before)

        # Manifest claims expanded_bytes=0 but archive contains file bytes.
        bomb = self._store_bytes({"secret": b"payload-bytes"})
        reader = MemoryS3({
            self.mod.manifest_object_key(key): json.dumps(
                self._manifest(key, bomb, expanded=0)
            ).encode(),
            self.mod.archive_object_key(key): bomb,
        })
        before = dict(reader.objects)
        result = self.mod.restore_store(self.root, key, reader, scope="full", toolchain=self.toolchain)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.reason, "expanded_mismatch")
        self.assertEqual(reader.objects, before)
        self.assertFalse((self.root / ".pnpm-store").exists())

    def test_reject_incomplete_manifest_fields(self):
        key = self._key()
        archive = self._store_bytes({"ok": b"data"})
        incomplete = {
            "schema": 1,
            "key": key,
            # missing toolchain/scope/created_at/expanded
            "archive_sha256": hashlib.sha256(archive).hexdigest(),
            "compressed_bytes": len(archive),
        }
        reader = MemoryS3({
            self.mod.manifest_object_key(key): json.dumps(incomplete).encode(),
            self.mod.archive_object_key(key): archive,
        })
        result = self.mod.restore_store(self.root, key, reader, scope="full", toolchain=self.toolchain)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.reason, "corrupt_manifest")

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            link = tarfile.TarInfo(name="link")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            tar.addfile(link)
        archive = buf.getvalue()
        reader = MemoryS3({
            self.mod.manifest_object_key(key): json.dumps(
                self._manifest(key, archive, expanded=0)
            ).encode(),
            self.mod.archive_object_key(key): archive,
        })
        before = dict(reader.objects)
        result = self.mod.restore_store(self.root, key, reader, scope="full", toolchain=self.toolchain)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.reason, "unsafe_archive_member")
        self.assertEqual(reader.objects, before)

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            dev = tarfile.TarInfo(name="dev/null")
            dev.type = tarfile.CHRTYPE
            dev.devmajor = 1
            dev.devminor = 3
            tar.addfile(dev)
        archive = buf.getvalue()
        reader = MemoryS3({
            self.mod.manifest_object_key(key): json.dumps(
                self._manifest(key, archive, expanded=0)
            ).encode(),
            self.mod.archive_object_key(key): archive,
        })
        before = dict(reader.objects)
        result = self.mod.restore_store(self.root, key, reader, scope="full", toolchain=self.toolchain)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.reason, "unsafe_archive_member")
        self.assertEqual(reader.objects, before)

    def test_reject_corrupt_sha_expired_and_oversized(self):
        key = self._key()
        archive = self._store_bytes({"ok": b"data"})
        # Corrupt SHA
        reader = MemoryS3({
            self.mod.manifest_object_key(key): json.dumps(
                self._manifest(key, archive, expanded=4, corrupt_sha=True)
            ).encode(),
            self.mod.archive_object_key(key): archive,
        })
        before = dict(reader.objects)
        result = self.mod.restore_store(self.root, key, reader, scope="full", toolchain=self.toolchain)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.reason, "checksum_mismatch")
        self.assertEqual(reader.objects, before)

        # Expired
        reader = MemoryS3({
            self.mod.manifest_object_key(key): json.dumps(
                self._manifest(key, archive, expanded=4, created_at=time.time() - (8 * 86400))
            ).encode(),
            self.mod.archive_object_key(key): archive,
        })
        before = dict(reader.objects)
        result = self.mod.restore_store(self.root, key, reader, scope="full", toolchain=self.toolchain)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.reason, "expired")
        self.assertEqual(reader.objects, before)

        # Oversized compressed (>2Gi) claimed by manifest — reject before trusting body
        big = dict(self._manifest(key, archive, expanded=4))
        big["compressed_bytes"] = self.mod.MAX_COMPRESSED_BYTES + 1
        big["archive_sha256"] = hashlib.sha256(archive).hexdigest()
        reader = MemoryS3({
            self.mod.manifest_object_key(key): json.dumps(big).encode(),
            self.mod.archive_object_key(key): archive,
        })
        result = self.mod.restore_store(self.root, key, reader, scope="full", toolchain=self.toolchain)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.reason, "compressed_too_large")

    def test_reject_expanded_over_free_disk_budget(self):
        key = self._key()
        archive = self._store_bytes({"ok": b"data"})
        manifest = self._manifest(key, archive, expanded=10_000)
        reader = MemoryS3({
            self.mod.manifest_object_key(key): json.dumps(manifest).encode(),
            self.mod.archive_object_key(key): archive,
        })
        with mock.patch.object(self.mod, "free_disk_bytes", return_value=100):
            result = self.mod.restore_store(self.root, key, reader, scope="full", toolchain=self.toolchain)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.reason, "expanded_exceeds_free_disk")

    def test_miss_when_object_absent(self):
        key = self._key()
        result = self.mod.restore_store(self.root, key, MemoryS3(), scope="full", toolchain=self.toolchain)
        self.assertEqual(result.status, "miss")
        self.assertEqual(result.reason, "not_found")

    def test_publish_store_is_pure_library_without_trust_flags(self):
        store = self.root / ".pnpm-store"
        store.mkdir()
        (store / "x").write_bytes(b"payload")
        key = self._key()
        writer = MemoryS3()
        # Caller trust/env flags must not be consulted as authorization.
        with mock.patch.dict(os.environ, {"AJRLY_TRUSTED": "true", "AJRLY_PIPELINE_MODE": "trusted"}):
            # Even if someone patches a would-be authority, publish_store must not call it.
            if hasattr(self.mod, "authorize_writer"):
                with mock.patch.object(self.mod, "authorize_writer") as auth:
                    result = self.mod.publish_store(
                        self.root, key, writer, scope="full", toolchain=self.toolchain
                    )
                    auth.assert_not_called()
            else:
                result = self.mod.publish_store(
                    self.root, key, writer, scope="full", toolchain=self.toolchain
                )
        self.assertEqual(result["schema"], 1)
        self.assertEqual(result["key"], key)
        self.assertIn("archive_sha256", result)
        self.assertLessEqual(result["compressed_bytes"], self.mod.MAX_COMPRESSED_BYTES)
        self.assertTrue(any(k.endswith(".tar.gz") for k, _, _ in writer.puts))
        self.assertTrue(any(k.endswith(".manifest.json") for k, _, _ in writer.puts))

    def test_publish_rejects_store_outside_pnpm_store_and_oversize(self):
        key = self._key()
        writer = MemoryS3()
        with self.assertRaises(ValueError):
            self.mod.publish_store(self.root, key, writer, scope="full", toolchain=self.toolchain)
        store = self.root / ".pnpm-store"
        store.mkdir()
        (store / "big").write_bytes(b"x" * 1024)
        with mock.patch.object(self.mod, "MAX_COMPRESSED_BYTES", 10):
            with self.assertRaises(ValueError) as raised:
                self.mod.publish_store(self.root, key, writer, scope="full", toolchain=self.toolchain)
        self.assertIn("compressed", str(raised.exception).lower())

    def test_summaries_omit_secrets_and_urls_with_credentials(self):
        key, reader = self._publish_local({"a": b"1"})
        shutil.rmtree(self.root / ".pnpm-store")
        result = self.mod.restore_store(self.root, key, reader, scope="full", toolchain=self.toolchain)
        summary = self.mod.format_restore_summary(result, key=key)
        self.assertIn(result.status, summary)
        self.assertNotIn("AKIA", summary)
        self.assertNotIn("secret", summary.lower())
        self.assertNotIn("X-Amz-Signature", summary)

    def test_registry_config_prefers_install_token_and_private_registry(self):
        cfg = self.mod.registry_config(
            {
                "AJRLY_NPM_REGISTRY": "https://npm.ajrly.com/",
                "AJRLY_NPM_INSTALL_TOKEN": "install-only-token",
                "NPM_TOKEN": "publisher-must-not-win",
                "PNPM_CONFIG_REGISTRY": "https://registry.npmjs.org/",
            }
        )
        self.assertEqual(cfg["registry"], "https://npm.ajrly.com/")
        self.assertEqual(cfg["auth_token"], "install-only-token")
        self.assertTrue(cfg["private"])
        # Public fallback when private registry not configured
        public = self.mod.registry_config({"PNPM_CONFIG_REGISTRY": "https://registry.npmjs.org/"})
        self.assertEqual(public["registry"], "https://registry.npmjs.org/")
        self.assertFalse(public["private"])
        self.assertEqual(public["auth_token"], "")

    def test_path_helper_encodes_templates_script(self):
        pin = "b318ecdf953767b2b7ac171b944a7646c37b250d"
        url = self.mod.templates_script_raw_url(
            "http://192.168.68.12/api/v4/projects/1", pin, "dependency_cache.py"
        )
        self.assertIn("ajrly%2Fscripts%2Fdependency_cache.py", url)
        self.assertIn(f"ref={pin}", url)
        self.assertEqual(
            self.mod.TEMPLATES_SCRIPT_RELATIVE["dependency_cache"],
            "ajrly/scripts/dependency_cache.py",
        )

    def test_node_yml_restores_after_source_without_cache_key_files_or_upload(self):
        text = NODE_YML.read_text()
        data = yaml.load(text, Loader=GitLabLoader)
        node = data[".node"]
        self.assertNotIn("cache", node)
        self.assertNotRegex(text, r"(?m)^\s*cache:\s*$")
        self.assertNotRegex(text, r"(?m)^\s*key:\s*\n\s*files:")
        def flatten(items):
            for item in items:
                if isinstance(item, list):
                    yield from flatten(item)
                else:
                    yield str(item)

        script_items = list(flatten(node["before_script"]))
        script = "\n".join(script_items)
        # !reference [.ajrly_source, before_script] must appear before dependency_cache fetch/restore.
        ref_idx = next(i for i, item in enumerate(script_items) if "ajrly_source" in item)
        fetch_idx = next(i for i, item in enumerate(script_items) if "ajrly%2Fscripts%2Fdependency_cache.py" in item)
        restore_idx = next(
            i for i, item in enumerate(script_items)
            if "ajrly-dependency-cache.py restore" in item or "/tmp/ajrly-dependency-cache.py restore" in item
        )
        self.assertLess(ref_idx, fetch_idx)
        self.assertLess(fetch_idx, restore_idx, "exact checkout must precede key/restore")
        self.assertIn("restore", script)
        self.assertNotIn("publish_store", script)
        self.assertNotRegex(script, r"(?m)^\s*python3 .*\bpublish\b")
        variables = node["variables"]
        self.assertEqual(variables.get("AJRLY_DEPENDENCY_CACHE_ENABLED"), "false")
        self.assertIn("AJRLY_DEPENDENCY_CACHE_PREFIX", variables)
        self.assertEqual(variables["AJRLY_DEPENDENCY_CACHE_PREFIX"], "canonical/")
        # Cutover gated: public registry remains default; private is opt-in.
        self.assertEqual(variables["PNPM_CONFIG_REGISTRY"], "https://registry.npmjs.org/")
        self.assertEqual(variables.get("AJRLY_NPM_REGISTRY", ""), "")
        self.assertNotIn("AJRLY_CI_TOOL_IMAGE_DIGEST", variables)
        self.assertEqual(variables.get("PNPM_CONFIG_NETWORK_CONCURRENCY"), "4")

    def test_canary_pins_verified_digest_without_global_flag_cutover(self):
        self.assertTrue(CANARY_YML.is_file(), "node-cache-consumer canary include missing")
        text = CANARY_YML.read_text()
        data = yaml.load(text, Loader=GitLabLoader)
        self.assertIn(VERIFIED_NODE_DIGEST, text)
        self.assertTrue(VERIFIED_NODE_DIGEST.endswith("becee899"))
        # Include-level defaults remain disabled (no global cutover).
        self.assertEqual(data["variables"]["AJRLY_DEPENDENCY_CACHE_ENABLED"], "false")
        self.assertEqual(data["variables"]["AJRLY_TURBO_BROKER_ENABLED"], "false")
        self.assertEqual(data["variables"]["AJRLY_TURBO_WRITE_ENABLED"], "false")
        # Job-scoped enable only on consumer base / install jobs.
        base = data[".node_cache_consumer_base"]
        self.assertEqual(base["variables"]["GIT_STRATEGY"], "none")
        self.assertEqual(base["variables"]["AJRLY_DEPENDENCY_CACHE_ENABLED"], "true")
        self.assertEqual(base["variables"]["AJRLY_NPM_REGISTRY"], "https://npm.ajrly.com/")
        # Must not override include/project immutable SHA pin with a mutable branch tip.
        templates_ref = str(base["variables"].get("AJRLY_TEMPLATES_REF", ""))
        self.assertNotEqual(templates_ref, "codex/ci-cache-templates")
        self.assertTrue(
            re.fullmatch(r"[0-9a-f]{40}", templates_ref) or templates_ref == "",
            "AJRLY_TEMPLATES_REF must be empty (inherit pin) or an immutable 40-char SHA",
        )
        # Runner config allows IfNotPresent only (Always was rejected on pipeline 459).
        self.assertEqual(base["image"]["pull_policy"], "if-not-present")
        self.assertNotIn("cache", base)
        self.assertNotRegex(text, r"(?m)^\s*cache:\s*$")
        self.assertNotRegex(text, r"(?m)^\s*paths:\s*\n\s*-\s*node_modules")
        # Ordering: exact checkout before restore in before_script.
        steps = [str(s) for s in base["before_script"]]
        checkout_idx = next(i for i, s in enumerate(steps) if "ajrly-source.py checkout" in s)
        restore_idx = next(i for i, s in enumerate(steps) if "dependency-cache.py restore" in s or "ajrly-dependency-cache.py restore" in s)
        self.assertLess(checkout_idx, restore_idx)
        before = "\n".join(steps)
        self.assertIn("require-immutable-ref", before)
        self.assertIn("record-pinned-raw", before)
        self.assertIn("transport_bound_pin", before)
        # JOB-TOKEN metadata JSON 404 is a real policy; canary must not block on it.
        self.assertNotRegex(before, r"(?m)^\s*[^#]*\bfile-metadata\b")
        self.assertNotIn("--upstream-host npm.ajrly.com", text)
        self.assertIn("configured_registry", text)
        self.assertIn("observed_registry_hosts", text)
        self.assertIn("--pnpm-progress", text)
        self.assertIn("--configured-registry", text)
        # No ordinary writer / turbo broker enable on canary jobs (comments may deny-list).
        self.assertNotRegex(text, r"(?m)^\s*[^#]*\bpublish_store\b")
        self.assertNotIn("AJRLY_TURBO_BROKER_ENABLED: \"true\"", text)
        # Publisher token must never be declared as a CI variable (denial checks may mention it).
        self.assertNotRegex(text, r"(?m)^\s*NPM_TOKEN:\s*")
        self.assertIn('publisher NPM_TOKEN must not be present', text)
        # Dedicated jobs: cold + 2 warm + sources-only + lock/libc/arch invalidation.
        for name in (
            "node-cache-canary-cold",
            "node-cache-canary-warm",
            "node-cache-canary-warm2",
            "node-cache-canary-sources-only",
            "node-cache-canary-invalidate-lock",
            "node-cache-canary-invalidate-libc",
            "node-cache-canary-invalidate-arch",
        ):
            self.assertIn(name, data)
        self.assertEqual(data["node-cache-canary-warm"]["needs"], ["node-cache-canary-cold"])
        self.assertEqual(data["node-cache-canary-warm2"]["needs"], ["node-cache-canary-warm"])
        self.assertEqual(
            data["node-cache-canary-sources-only"]["needs"], ["node-cache-canary-warm2"]
        )
        # Build-pool Node tag only (checks pool not required for this consumer).
        self.assertEqual(base["tags"], ["ajrly-kubernetes"])
        self.assertNotIn("ajrly-kubernetes-checks", text)
        # Full pnpm install (trusted source scripts risk), not fetch-only.
        install_script = "\n".join(str(s) for s in data[".node_cache_consumer_install"]["script"])
        self.assertIn("pnpm install", install_script)
        self.assertIn("local_store_warm_not_persistent", install_script)
        self.assertIn("persistent_canonical_restore", install_script)
        self.assertNotIn("publish_store", install_script)
        # libc/arch probes must register importlib module before exec (dataclasses).
        libc_script = "\n".join(str(s) for s in data["node-cache-canary-invalidate-libc"]["script"])
        arch_script = "\n".join(str(s) for s in data["node-cache-canary-invalidate-arch"]["script"])
        self.assertIn("sys.modules[spec.name] = mod", libc_script)
        self.assertIn("sys.modules[spec.name] = mod", arch_script)
        # Must not be wired into the default reusable pipeline.
        pipeline_text = PIPELINE_YML.read_text()
        self.assertNotIn("node-cache-consumer.yml", pipeline_text)
        self.assertNotIn("canary/node-cache-consumer", pipeline_text)
        # Default .node remains gated off after canary addition.
        node_vars = yaml.load(NODE_YML.read_text(), Loader=GitLabLoader)[".node"]["variables"]
        self.assertEqual(node_vars["AJRLY_DEPENDENCY_CACHE_ENABLED"], "false")
        self.assertEqual(node_vars.get("AJRLY_NPM_REGISTRY", ""), "")
        self.assertNotIn("AJRLY_CI_TOOL_IMAGE_DIGEST", node_vars)

class JobInstallScopeTests(unittest.TestCase):
    """Job-definition install scope is authoritative; no fabricated full-scope keys."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("dependency_cache_job_scope", SCRIPT)
        cls.mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        # Register before exec so @dataclass can resolve the module namespace (Py3.12+/Alpine).
        sys.modules[spec.name] = cls.mod
        spec.loader.exec_module(cls.mod)

    def test_resolve_job_install_scope_reads_project_path_over_cache_scope_flag(self):
        # Caller/write flag AJRLY_DEPENDENCY_CACHE_SCOPE=full must not widen a scoped job.
        scope = self.mod.resolve_job_install_scope({
            "PROJECT_PATH": "apps/backend/api",
            "AJRLY_DEPENDENCY_CACHE_SCOPE": "full",
            "AJRLY_DEPENDENCY_CACHE_WRITE": "true",  # ignored noise
        })
        self.assertEqual(scope, "apps/backend/api")

    def test_resolve_job_install_scope_packages_and_full(self):
        self.assertEqual(
            self.mod.resolve_job_install_scope({"PNPM_INSTALL_SCOPE": "packages"}),
            "packages",
        )
        self.assertEqual(
            self.mod.resolve_job_install_scope({"PNPM_INSTALL_SCOPE": "full"}),
            "full",
        )

    def test_resolve_job_install_scope_cache_scope_only_when_no_install_vars(self):
        self.assertEqual(
            self.mod.resolve_job_install_scope({"AJRLY_DEPENDENCY_CACHE_SCOPE": "full"}),
            "full",
        )
        self.assertEqual(self.mod.resolve_job_install_scope({}), "full")

    def test_resolve_job_install_scope_rejects_traversal(self):
        with self.assertRaises(ValueError):
            self.mod.resolve_job_install_scope({"PROJECT_PATH": "../etc"})

    def test_restore_reuses_full_archive_without_fabricating_job_scope_key(self):
        """Scoped job may restore the canonical full archive; report must keep scopes distinct."""
        root = pathlib.Path(tempfile.mkdtemp(prefix="scope-restore-"))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        (root / ".ajrly-source.json").write_text(
            json.dumps({"sha": "a" * 40}) + "\n", encoding="utf-8"
        )
        (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
        (root / "pnpm-workspace.yaml").write_text("packages: []\n", encoding="utf-8")
        store = root / ".pnpm-store"
        store.mkdir()
        (store / "v3").mkdir()
        (store / "v3" / "files").write_bytes(b"canonical-store")

        toolchain = {"node": "26.11.1", "pnpm": "12.10.1"}
        full_key = self.mod.dependency_key(root, "full", toolchain)
        job_scope = "apps/backend/api"
        job_key = self.mod.dependency_key(root, job_scope, toolchain)
        self.assertNotEqual(full_key, job_key)

        writer = MemoryS3()
        # Publish only the full archive (no scoped archive exists).
        self.mod.publish_store(
            root, full_key, writer, scope="full", toolchain=toolchain
        )
        # Clear local store so restore must fetch.
        shutil.rmtree(store)

        outcome = self.mod.restore_for_job_scope(
            root,
            writer,
            env={
                "PROJECT_PATH": job_scope,
                "AJRLY_DEPENDENCY_CACHE_SCOPE": "full",  # must not fabricate job key=full
            },
            toolchain=toolchain,
        )
        self.assertEqual(outcome.result.status, "hit")
        self.assertEqual(outcome.job_scope, job_scope)
        self.assertEqual(outcome.archive_scope, "full")
        self.assertEqual(outcome.archive_key, full_key)
        self.assertEqual(outcome.job_key, job_key)
        self.assertNotEqual(outcome.job_key, outcome.archive_key)
        self.assertEqual(outcome.result.reason, "reused_full_archive")
        self.assertTrue((root / ".pnpm-store" / "v3" / "files").is_file())

        report = self.mod.format_restore_report(
            outcome.result,
            key=outcome.archive_key,
            job_scope=outcome.job_scope,
            archive_scope=outcome.archive_scope,
            job_key=outcome.job_key,
        )
        self.assertEqual(report["job_scope"], job_scope)
        self.assertEqual(report["archive_scope"], "full")
        self.assertEqual(report["job_key_prefix"], job_key[:18] + "…")
        # Must not claim the job key equals the full archive key.
        self.assertNotEqual(report.get("job_key_prefix"), report.get("key_prefix"))

    def test_node_restore_uses_scope_from_job_not_caller_cache_scope_flag(self):
        text = NODE_YML.read_text()
        self.assertIn("--scope-from-job", text)
        # Must not prefer a caller-writable cache-scope default over job install vars.
        self.assertNotIn(
            '--scope "${AJRLY_DEPENDENCY_CACHE_SCOPE:-full}"',
            text,
        )

    def test_canary_last_key_uses_resolved_job_scope_not_hardcoded_full(self):
        text = CANARY_YML.read_text()
        # Hardcoding --scope full for LAST_KEY after restore fabricates identity.
        self.assertNotIn(
            'AJRLY_DEPENDENCY_CACHE_LAST_KEY=$(python3 /tmp/ajrly-dependency-cache.py key --root "$CI_PROJECT_DIR" --scope full)',
            text,
        )
        self.assertIn("--scope-from-job", text)

    def test_scoped_install_canary_include_exists_and_is_not_default_pipeline(self):
        canary = ROOT / "canary" / "node-scoped-install.yml"
        self.assertTrue(canary.is_file(), "dedicated scoped-install canary missing")
        text = canary.read_text()
        data = yaml.load(text, Loader=GitLabLoader)
        self.assertIn("PROJECT_PATH", text)
        self.assertIn("apps/backend/api", text)
        self.assertIn("pnpm-install-scoped", text)
        self.assertIn(VERIFIED_NODE_DIGEST, text)
        self.assertIn("--scope-from-job", text)
        self.assertIn("record-pinned-raw", text)
        self.assertIn("transport_bound_pin", text)
        self.assertIn("cold_skip_restore", text)
        self.assertIn("--filter api...", text)
        self.assertIn("--filter .", text)
        self.assertNotRegex(text, r"(?m)^\s*[^#]*\bpnpm\s+.*--ignore-scripts")
        self.assertNotRegex(text, r"(?m)^\s*[^#]*\bfile-metadata\b")
        self.assertNotIn("verify\":\"dual_raw\"", text)
        for name in (
            "node-scoped-install-canary-cold",
            "node-scoped-install-canary-warm",
            "node-scoped-install-canary-warm2",
            "node-scoped-install-canary-sources-only",
        ):
            self.assertIn(name, data)
        self.assertEqual(
            data["node-scoped-install-canary-cold"]["variables"]["AJRLY_DEPENDENCY_CACHE_SKIP_RESTORE"],
            "true",
        )
        self.assertEqual(
            data["node-scoped-install-canary-warm"]["needs"],
            ["node-scoped-install-canary-cold"],
        )
        self.assertEqual(
            data["node-scoped-install-canary-warm2"]["needs"],
            ["node-scoped-install-canary-warm"],
        )
        pipeline = PIPELINE_YML.read_text()
        self.assertNotIn("node-scoped-install.yml", pipeline)
        # .node remains free of metadata-call blocks when dependency cache is gated.
        node_text = NODE_YML.read_text()
        self.assertNotRegex(node_text, r"(?m)^\s*[^#]*\bfile-metadata\b")




if __name__ == "__main__":
    unittest.main()
