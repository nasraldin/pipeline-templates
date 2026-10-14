import pathlib
import shlex
import subprocess
import unittest
import yaml

ROOT = pathlib.Path(__file__).parents[1]


class GitLabLoader(yaml.SafeLoader):
    pass


GitLabLoader.add_constructor("!reference", lambda loader, node: loader.construct_sequence(node))


def merged(path):
    value = yaml.load(path.read_text(), Loader=GitLabLoader)
    result = {}
    for item in value.pop("include", []):
        result.update(merged(ROOT.parent / item["local"]))
    result.update(value)
    return result


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "pipeline.yml").exists(), "Ajrly reusable entry point missing")
        self.pipeline = merged(ROOT / "pipeline.yml")

    def test_all_apps_and_analysis_projects_preserved(self):
        apps = self.pipeline["apps"]["parallel"]["matrix"]
        self.assertEqual(len(apps), 9)
        self.assertEqual(len([key for key in self.pipeline if key.startswith("sonar:")]), 23)

    def test_checks_can_run_without_global_serialization(self):
        for key, job in self.pipeline.items():
            if isinstance(job, dict):
                self.assertNotEqual(job.get("resource_group"), "ajrly-platform-ci")
        self.assertEqual(self.pipeline["default"]["tags"], ["ajrly-kubernetes"])
        self.assertEqual(self.pipeline[".node"]["needs"], [])

    def test_services_are_disposable_and_local(self):
        env = self.pipeline[".integration"]["variables"]
        self.assertEqual(env["DATABASE_HOST"], "127.0.0.1")
        self.assertEqual(env["REDIS_ENDPOINT"], "redis://127.0.0.1:6379")

    def test_publication_requires_separate_trusted_mode(self):
        for name in ["image-build", "changeset-version", "sonar:api"]:
            rules = self.pipeline[name]["rules"]
            self.assertIn('AJRLY_PIPELINE_MODE == "trusted"', rules[0]["if"])
            self.assertEqual(rules[-1], {"when": "never"})

    def test_artifact_shell_escaping(self):
        commands = self.pipeline["apps"]["after_script"]
        find = next(command for command in commands if command.startswith("find "))
        self.assertEqual(shlex.split(find)[-1], ";", "find -exec must receive its terminator")
        fallback = next(command for command in commands if "printf" in command)
        expression = fallback.split("else ", 1)[1].split(" >", 1)[0]
        output = subprocess.check_output(["sh", "-c", expression])
        self.assertEqual(output, b"{}\n")

    def test_native_typechecker_has_a_budget_below_the_container_limit(self):
        env = self.pipeline[".node"]["variables"]
        self.assertEqual(env.get("GOMAXPROCS"), "2")
        self.assertEqual(env.get("GOMEMLIMIT"), "1GiB")
        self.assertEqual(env.get("PNPM_CONFIG_NETWORK_CONCURRENCY"), "4")
        # Feature flags are pipeline-level so project-7 canaries can override.
        self.assertNotIn("AJRLY_DEPENDENCY_CACHE_ENABLED", env)
        self.assertNotIn("AJRLY_TURBO_BROKER_ENABLED", env)
        globals_env = self.pipeline["variables"]
        self.assertEqual(globals_env.get("AJRLY_DEPENDENCY_CACHE_ENABLED"), "false")
        self.assertEqual(globals_env.get("AJRLY_TURBO_BROKER_ENABLED"), "false")
        self.assertEqual(globals_env.get("AJRLY_TURBO_WRITE_ENABLED"), "false")
        self.assertNotIn("cache", self.pipeline[".node"])

    def test_final_acceptance_canary_enables_contracts_without_writers(self):
        path = ROOT / "canary" / "ci-cache-final-acceptance.yml"
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertIn("AJRLY_DEPENDENCY_CACHE_ENABLED: \"true\"", text)
        self.assertIn("AJRLY_TURBO_WRITE_ENABLED: \"false\"", text)
        self.assertIn("AJRLY_BUILDKIT_READY: \"false\"", text)
        self.assertIn("codex/ci-cache-final-acceptance", text)
        self.assertIn("sha256:7a5a6894d32a279390b9c27a16feee922297a0e942b3c6e3900e11b0becee899", text)
        self.assertNotIn("ajrly/quality/sonar.yml", text)
        self.assertNotIn("ajrly/images/harbor.yml", text)

    def test_resource_profiles_bound_builds(self):
        variables = self.pipeline[".ajrly_build_resources"]["variables"]
        self.assertEqual(variables["KUBERNETES_MEMORY_REQUEST"], "1920Mi")
        self.assertEqual(variables["KUBERNETES_CPU_REQUEST"], "500m")
        self.assertEqual(variables["KUBERNETES_MEMORY_LIMIT"], "4Gi")
        self.assertEqual(self.pipeline["variables"]["GIT_STRATEGY"], "none")


if __name__ == "__main__":
    unittest.main()
