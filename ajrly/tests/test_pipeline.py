import pathlib
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

    def test_resource_profiles_bound_builds(self):
        variables = self.pipeline[".ajrly_build_resources"]["variables"]
        self.assertEqual(variables["KUBERNETES_MEMORY_REQUEST"], "2Gi")
        self.assertEqual(variables["KUBERNETES_MEMORY_LIMIT"], "4Gi")
        self.assertEqual(self.pipeline["variables"]["GIT_STRATEGY"], "none")


if __name__ == "__main__":
    unittest.main()
