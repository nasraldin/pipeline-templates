"""Failing-first tests for verified pnpm-store dependency archive restore."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import tarfile
import tempfile
import time
import unittest
from typing import Dict, Optional
from unittest import mock

import yaml

SCRIPT = pathlib.Path(__file__).parents[1] / "scripts/dependency_cache.py"
NODE_YML = pathlib.Path(__file__).parents[1] / "common/node.yml"
ROOT = pathlib.Path(__file__).parents[1]


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

    def test_key_changes_for_lock_workspace_patch_toolchain_not_source(self):
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

        scope_key = self.mod.dependency_key(self.root, "apps/web/admin", self.toolchain)
        self.assertNotEqual(base, scope_key)
        self.assertTrue(base.startswith("dep-v1-"))
        self.assertRegex(base, r"^dep-v1-[0-9a-f]{64}$")

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
        url = self.mod.templates_script_raw_url(
            "http://192.168.68.12/api/v4/projects/1", "codex/ci-cache-templates", "dependency_cache.py"
        )
        self.assertIn("ajrly%2Fscripts%2Fdependency_cache.py", url)
        self.assertIn("ref=codex%2Fci-cache-templates", url)
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


if __name__ == "__main__":
    unittest.main()
