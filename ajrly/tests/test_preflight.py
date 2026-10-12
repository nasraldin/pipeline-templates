import importlib.util
import pathlib
import unittest


class PreflightTests(unittest.TestCase):
    def test_fixture_can_override_external_dispatch_workflow(self):
        path = pathlib.Path(__file__).parents[2] / "ci/preflight/validate.py"
        spec = importlib.util.spec_from_file_location("preflight", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        text = module.build_template_fixture(dict(id="external", template="ajrly/pipeline.yml",
                                                stages=["validate"], jobs=[], workflow_always=True))
        self.assertIn("workflow:\n  rules:\n    - when: always", text)
