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
        self.assertEqual(env.get("AJRLY_DEPENDENCY_CACHE_ENABLED"), "false")
        self.assertEqual(env.get("AJRLY_TURBO_BROKER_ENABLED"), "false")
        self.assertEqual(env.get("AJRLY_TURBO_WRITE_ENABLED"), "false")
        self.assertNotIn("cache", self.pipeline[".node"])

    def test_resource_profiles_bound_builds(self):
        variables = self.pipeline[".ajrly_build_resources"]["variables"]
        self.assertEqual(variables["KUBERNETES_MEMORY_REQUEST"], "1920Mi")
        self.assertEqual(variables["KUBERNETES_CPU_REQUEST"], "500m")
        self.assertEqual(variables["KUBERNETES_MEMORY_LIMIT"], "4Gi")
        self.assertEqual(self.pipeline["variables"]["GIT_STRATEGY"], "none")


if __name__ == "__main__":
    unittest.main()
