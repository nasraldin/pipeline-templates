import importlib.util
import pathlib
import subprocess
import tempfile
import unittest

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
        self.payload.update(event="pull_request", pr_number="7", ref="refs/pull/7/head")
        self.metadata.update(ref="refs/pull/7/head", head_repository="nasraldin/ajrly-platform",
                             target_sha="b" * 40, target_ref="refs/heads/main")
        result = self.source.validate_event(self.payload, self.metadata)
        self.assertFalse(result["trusted"])
        self.assertEqual(result["target_sha"], "b" * 40)

    def test_fork_fails_closed(self):
        self.payload.update(event="pull_request", pr_number="7", ref="refs/pull/7/head")
        self.metadata.update(ref="refs/pull/7/head", head_repository="someone/fork")
        with self.assertRaises(ValueError):
            self.source.validate_event(self.payload, self.metadata)

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
