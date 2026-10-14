import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import yaml


class RegistryEnvironmentTests(unittest.TestCase):
    def test_private_registry_wins_over_public_pnpm_environment_default(self):
        class Loader(yaml.SafeLoader):
            pass
        Loader.add_constructor("!reference", lambda loader, node: loader.construct_sequence(node))
        root = Path(__file__).resolve().parents[1]
        node = yaml.load((root / "common/node.yml").read_text(), Loader=Loader)[".node"]
        block = next(step for step in node["before_script"] if isinstance(step, str) and 'if [ -n "${AJRLY_NPM_REGISTRY' in step)
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            helper = folder / "dependency_cache.py"
            shutil.copyfile(root / "scripts/dependency_cache.py", helper)
            block = block.replace("/tmp/ajrly-dependency-cache.py", str(helper))
            env = dict(os.environ, CI_PROJECT_DIR=temp, AJRLY_NPM_REGISTRY="https://npm.ajrly.com/", AJRLY_NPM_INSTALL_TOKEN="fixture-read-only-token", PNPM_CONFIG_REGISTRY="https://registry.npmjs.org/")
            result = subprocess.run(["bash", "-ec", block + "\npnpm config get registry"], env=env, cwd=temp, capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip().splitlines()[-1], "https://npm.ajrly.com/")


if __name__ == "__main__":
    unittest.main()
