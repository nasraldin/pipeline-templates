import importlib.util
import pathlib
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('preflight', pathlib.Path(__file__).with_name('validate.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RootLintTests(unittest.TestCase):
    def test_open_mr_root_lint_uses_default_branch_context(self):
        def lint(_content, ref=None):
            return {'valid': ref == 'main', 'errors': ['duplicate branch pipeline'], 'jobs': [], 'merged_yaml': ''}
        with patch.dict(module.os.environ, {'CI_COMMIT_REF_NAME': 'feature', 'CI_DEFAULT_BRANCH': 'main'}), patch.object(module, 'ci_lint', side_effect=lint), patch.object(module, 'compare_expected', return_value=[]):
            self.assertEqual(module.run_root(False), [('root-gitlab-ci', [])])
