"""Declared release metadata agrees before preparing a clean release archive."""
import fnmatch
from pathlib import Path
import re
import sys
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from askai_cli import __version__


class ReleaseContractTests(unittest.TestCase):
    def test_versions_and_formula_dependency_resources_agree_with_lock(self):
        project = tomllib.loads((ROOT / 'pyproject.toml').read_text())
        packages = {p['name']: p for p in tomllib.loads((ROOT / 'uv.lock').read_text())['package']}
        formula = (ROOT / 'packaging/homebrew/askai.rb').read_text()
        self.assertEqual(project['project']['version'], __version__)
        self.assertEqual(packages['askai-cli']['version'], __version__)
        self.assertIn(f'version "{__version__}"', formula)
        self.assertIn(f'tag: "v{__version__}"', formula)
        resources = re.findall(r'resource "([^"]+)" do\s+url "([^"]+)"\s+sha256 "([^"]+)"', formula)
        self.assertEqual({name for name, _, _ in resources}, set(packages) - {'askai-cli'})
        for name, url, sha in resources:
            filename = url.rsplit('/', 1)[-1]
            self.assertTrue(filename.startswith(name.replace('-', '_') + '-' + packages[name]['version'] + '-'))
            self.assertTrue(any(w.get('hash') == 'sha256:' + sha for w in packages[name]['wheels']))
        patterns = project['tool']['setuptools']['package-data']['askai_cli']
        for file in (ROOT / 'src/askai_cli/prompts').iterdir():
            self.assertTrue(any(fnmatch.fnmatch(str(file.relative_to(ROOT / 'src/askai_cli')), p) for p in patterns))


if __name__ == '__main__':
    unittest.main()
