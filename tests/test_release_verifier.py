"""Release candidates are checked read-only, including exact refs and contents."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_verifier', ROOT / 'scripts/check-release.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class ReleaseVerifierTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='askai-release-check-')
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name) / 'candidate'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Release Test')
        self.git('config', 'user.email', 'release@example.invalid')
        (self.repo / 'base').write_text('base')
        self.commit('Release base')
        self.parent = self.git('rev-parse', 'HEAD')
        self.write('pyproject.toml', '[project]\nversion="1.2.3"\n')
        self.write('src/askai_cli/__init__.py', '__version__ = "1.2.3"\n')
        self.write('uv.lock', '[[package]]\nname="askai-cli"\nversion="1.2.3"\n')
        self.formula = 'class Askai < Formula\n  version "1.2.3"\n  url "https://example.invalid/repo", tag: "v1.2.3"\nend\n'
        self.write('packaging/homebrew/askai.rb', self.formula)
        self.commit('Release v1.2.3')
        self.head = self.git('rev-parse', 'HEAD')
        self.git('tag', 'v1.2.3')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.PIPE, text=True).strip()

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, message):
        self.git('add', '-A')
        self.git('commit', '-qm', message)

    def verify(self, **kwargs):
        return checker.verify(self.repo, tag='v1.2.3', parent=self.parent, **kwargs)

    def test_exact_clean_candidate_passes_without_modifying_refs(self):
        before = self.git('show-ref')
        result = self.verify()
        self.assertEqual(result['commit'], self.head)
        self.assertIn('not checked', result['remote'])
        self.assertEqual(self.git('show-ref'), before)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_ignored_nested_directory_cannot_borrow_outer_git_tree(self):
        import shutil
        self.write('.gitignore', 'ignored/\n')
        self.git('add', '.gitignore')
        self.git('commit', '--amend', '--no-edit', '-q')
        self.git('tag', '-f', 'v1.2.3')
        nested = self.repo / 'ignored'
        for name in ['pyproject.toml', 'src/askai_cli/__init__.py', 'uv.lock', 'packaging/homebrew/askai.rb']:
            path = nested / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.repo / name, path)
        (nested / 'src/askai_cli/__init__.py').write_text('__version__="1.2.3"\n# not the committed source\n')
        self.assertEqual(self.git('status', '--porcelain'), '')
        with self.assertRaisesRegex(checker.ReleaseError, 'working tree root'):
            checker.verify(nested, tag='v1.2.3', parent=self.parent)

    def test_untracked_and_staged_files_block(self):
        self.write('forgotten.py', 'pass')
        with self.assertRaisesRegex(checker.ReleaseError, 'clean'):
            self.verify()
        self.git('add', 'forgotten.py')
        with self.assertRaisesRegex(checker.ReleaseError, 'clean'):
            self.verify()

    def test_committed_wrong_lock_fails_before_tag(self):
        self.write('uv.lock', '[[package]]\nname="askai-cli"\nversion="0.0.1"\n')
        self.git('add', 'uv.lock')
        self.git('commit', '--amend', '--no-edit', '-q')
        with self.assertRaisesRegex(checker.ReleaseError, 'versions'):
            self.verify()

    def test_existing_wrong_tag_and_missing_tag_fail(self):
        self.git('tag', '-d', 'v1.2.3')
        with self.assertRaisesRegex(checker.ReleaseError, 'missing'):
            self.verify()
        self.assertFalse(self.verify(allow_untagged=True)['tag_present'])
        self.git('tag', 'v1.2.3', self.parent)
        with self.assertRaisesRegex(checker.ReleaseError, 'another commit'):
            self.verify(allow_untagged=True)

    def test_private_intermediate_commit_is_not_accepted_as_ancestry(self):
        self.write('extra', 'development commit')
        self.commit('development')
        with self.assertRaisesRegex(checker.ReleaseError, 'directly after'):
            self.verify()

    def test_external_tap_formula_requires_exact_revision(self):
        formula = self.repo.parent / 'askai.rb'
        formula.write_text(self.formula)
        with self.assertRaisesRegex(checker.ReleaseError, 'revision'):
            self.verify(formula=formula)
        formula.write_text(self.formula.replace('tag: "v1.2.3"', 'tag: "v1.2.3", revision: "' + self.head + '"'))
        self.verify(formula=formula)
        formula.write_text(formula.read_text().replace(self.head, self.parent))
        with self.assertRaisesRegex(checker.ReleaseError, 'revision'):
            self.verify(formula=formula)

    def test_remote_query_failure_does_not_become_empty_inventory(self):
        real_git = checker.git
        def failed(repo, *args):
            if args[0] == 'ls-remote':
                raise checker.ReleaseError('remote unavailable')
            return real_git(repo, *args)
        with patch.object(checker, 'git', side_effect=failed):
            with self.assertRaisesRegex(checker.ReleaseError, 'remote unavailable'):
                self.verify(online=True)

    def test_online_local_fake_remote_checks_absent_conflicting_and_published_tag(self):
        remote = self.repo.parent / 'remote.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
        self.git('remote', 'add', 'origin', str(remote))
        self.git('push', '-q', 'origin', self.parent + ':refs/heads/main')
        self.assertIn('baseline confirmed', self.verify(online=True)['remote'])
        self.git('push', '-q', 'origin', self.parent + ':refs/tags/v1.2.3')
        with self.assertRaisesRegex(checker.ReleaseError, 'Remote tag points elsewhere'):
            self.verify(online=True)
        # Recreate only this owned test remote; production verifier never mutates it.
        subprocess.run(['git', '--git-dir', str(remote), 'update-ref', '-d', 'refs/tags/v1.2.3'], check=True)
        self.git('push', '-q', 'origin', 'HEAD:refs/heads/main', 'refs/tags/v1.2.3')
        self.assertEqual(self.verify(online=True)['remote'], 'already published')


if __name__ == '__main__':
    unittest.main()
