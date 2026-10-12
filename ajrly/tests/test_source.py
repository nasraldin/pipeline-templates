import importlib.util
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

SCRIPT = pathlib.Path(__file__).parents[1] / "scripts/source.py"


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.exists(), "verified source implementation is missing")
        spec = importlib.util.spec_from_file_location("ajrly_source", SCRIPT)
        self.source = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.source)
        self.payload = dict(repository="nasraldin/ajrly-platform", sha="a" * 40,
                            ref="refs/heads/main", event="push", event_id="42")
        self.metadata = dict(repository="nasraldin/ajrly-platform", sha="a" * 40,
                             ref="refs/heads/main", protected=True)

    def test_transient_git_fetch_retries_without_changing_identity(self):
        error = subprocess.CalledProcessError(128, ["git"], stderr="error: RPC failed; curl 92 HTTP/2 stream reset")
        success = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
        with mock.patch.object(self.source.subprocess, "run", side_effect=[error, success]) as run, \
                mock.patch.object(self.source.time, "sleep"):
            self.source.git_fetch(pathlib.Path("."), ["--depth=1", "origin", "a" * 40], "fixture-token")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args, run.call_args_list[1].args)
        self.assertIn("http.version=HTTP/1.1", run.call_args.args[0])

    def test_git_auth_errors_are_not_retried_or_logged_with_credentials(self):
        token = "fixture-secret-token"
        error = subprocess.CalledProcessError(128, ["git"], stderr="fatal: Authentication failed " + token)
        with mock.patch.object(self.source.subprocess, "run", side_effect=error) as run, \
                self.assertRaises(RuntimeError) as raised:
            self.source.git_fetch(pathlib.Path("."), ["origin", "a" * 40], token)
        self.assertEqual(run.call_count, 1)
        self.assertNotIn(token, str(raised.exception))
        self.assertIn("Authentication failed", str(raised.exception))

    def test_protected_push_is_trusted(self):
        self.assertTrue(self.source.validate_event(self.payload, self.metadata)["trusted"])

    def test_supplied_trust_cannot_override_metadata(self):
        self.payload["trusted"] = True
        self.metadata["protected"] = False
        self.assertFalse(self.source.validate_event(self.payload, self.metadata)["trusted"])

    def test_moved_ref_fails(self):
        self.metadata["sha"] = "b" * 40
        with self.assertRaises(ValueError):
            self.source.validate_event(self.payload, self.metadata)

    def test_other_repository_fails(self):
        self.payload["repository"] = "attacker/repository"
        with self.assertRaises(ValueError):
            self.source.validate_event(self.payload, self.metadata)

    def test_invalid_sha_or_ref_fails(self):
        for key, value in [("sha", "--upload-pack=evil"), ("ref", "refs/heads/main:other")]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.source.validate_event(dict(self.payload, **{key: value}), self.metadata)

    def test_pull_request_never_receives_publication_trust(self):
        self.payload.update(event="pull_request", pr_number="7", ref="refs/pull/7/head", target_sha="b" * 40, target_ref="refs/heads/main")
        self.metadata.update(ref="refs/pull/7/head", head_repository="nasraldin/ajrly-platform",
                             target_sha="b" * 40, target_ref="refs/heads/main")
        result = self.source.validate_event(self.payload, self.metadata)
        self.assertFalse(result["trusted"])
        self.assertEqual(result["target_sha"], "b" * 40)

    def test_moved_pull_request_target_fails(self):
        self.payload.update(event="pull_request", pr_number="7", ref="refs/pull/7/head", target_sha="b" * 40, target_ref="refs/heads/main")
        self.metadata.update(ref="refs/pull/7/head", head_repository="nasraldin/ajrly-platform", target_sha="c" * 40, target_ref="refs/heads/main")
        with self.assertRaises(ValueError):
            self.source.validate_event(self.payload, self.metadata)

    def test_fork_fails_closed(self):
        self.payload.update(event="pull_request", pr_number="7", ref="refs/pull/7/head", target_sha="b" * 40, target_ref="refs/heads/main")
        self.metadata.update(ref="refs/pull/7/head", head_repository="someone/fork")
        with self.assertRaises(ValueError):
            self.source.validate_event(self.payload, self.metadata)

    def test_shared_changes_affect_every_app(self):
        self.assertTrue(self.source.is_affected(["packages/ui/src/index.ts"], "apps/web/admin"))
        self.assertTrue(self.source.is_affected(["pnpm-lock.yaml"], "apps/web/admin"))

    def test_unrelated_app_changes_skip_work(self):
        self.assertFalse(self.source.is_affected(["apps/web/blog/src/page.astro"], "apps/web/admin"))
        self.assertTrue(self.source.is_affected(["apps/web/admin/src/page.tsx"], "apps/web/admin"))

    def test_full_history_retains_changeset_merge_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            upstream = pathlib.Path(tmp) / "upstream"
            upstream.mkdir()
            def git(*args):
                return subprocess.check_output(["git", "-C", str(upstream), *args], text=True).strip()
            git("init", "-q")
            git("config", "user.email", "ci@example.invalid")
            git("config", "user.name", "CI test")
            commits = []
            for index in range(5):
                (upstream / "content").write_text(str(index))
                git("add", "content")
                git("commit", "-qm", "change")
                commits.append(git("rev-parse", "HEAD"))
            checkout = pathlib.Path(tmp) / "checkout"
            self.source.checkout_exact(str(upstream), "HEAD", commits[-1], checkout, full_history=True)
            base = subprocess.check_output(["git", "-C", str(checkout), "merge-base", commits[0], "HEAD"], text=True).strip()
            self.assertEqual(base, commits[0])

    def test_checkout_verifies_exact_sha_without_credentials_in_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            upstream = root / "upstream"
            upstream.mkdir()
            def git(*args):
                return subprocess.check_output(["git", "-C", str(upstream), *args], text=True).strip()
            git("init", "-q")
            git("config", "user.email", "ci@example.invalid")
            git("config", "user.name", "CI test")
            (upstream / "content").write_text("first")
            git("add", "content")
            git("commit", "-qm", "first")
            sha = git("rev-parse", "HEAD")
            destination = root / "checkout"
            self.source.checkout_exact(str(upstream), "HEAD", sha, destination)
            actual = subprocess.check_output(["git", "-C", str(destination), "rev-parse", "HEAD"], text=True).strip()
            self.assertEqual(actual, sha)
            with self.assertRaises(ValueError):
                self.source.checkout_exact(str(upstream), "HEAD", "c" * 40, root / "bad")


if __name__ == "__main__":
    unittest.main()
