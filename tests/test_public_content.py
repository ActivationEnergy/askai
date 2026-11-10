"""Public commit attribution is allowed; private paths and credentials are not."""

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("public_content", ROOT / "scripts/check-public-content.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class PublicContentTests(unittest.TestCase):
    def identity(self):
        return b"Project contributor <" + b"contributor" + b"@" + b"project.example>"

    def test_normal_author_committer_and_tagger_fields_are_allowed(self):
        person = self.identity() + b" 0 +0000"
        commit = b"tree 000\nauthor " + person + b"\ncommitter " + person + b"\n\nRelease\n"
        tag = b"object 000\ntype commit\ntag v1\ntagger " + person + b"\n\nRelease\n"
        self.assertEqual(checker.scan_git_object(commit, "commit", {b"contributor"}), set())
        self.assertEqual(checker.scan_git_object(tag, "tag", {b"contributor"}), set())

    def test_email_in_file_content_or_commit_message_still_requires_review(self):
        text = self.identity()
        self.assertIn("email address", checker.scan_git_object(text, "blob", set()))
        self.assertIn("email address", checker.scan_git_object(b"tree 000\n\nContact " + text, "commit", set()))

    def test_paths_and_tokens_in_identity_fields_are_still_rejected(self):
        path = b"/" + b"Users/" + b"sample-person/project"
        token = b"ghp_" + b"A" * 36
        for value, category in ((path, "absolute home directory"), (token, "possible access token")):
            raw = b"tree 000\nauthor " + value + b" <maintainer@example.invalid> 0 +0000\n\nRelease"
            self.assertIn(category, checker.scan_git_object(raw, "commit", set()))

    def test_local_identifier_in_content_is_not_treated_as_public_attribution(self):
        value = b"sample-workstation"
        self.assertIn("local user or host identifier", checker.scan_git_object(value, "blob", {value}))

    def test_complete_public_ssh_urls_are_not_emails_but_other_findings_remain(self):
        address = b"git" + b"@" + b"github.com"
        for url in (address + b":owner/repo.git", b"ssh://" + address + b"/owner/repo.git"):
            self.assertEqual(checker.scan(url, set()), set())
            self.assertIn("email address", checker.scan(url + b" contact " + self.identity(), set()))
        self.assertIn("email address", checker.scan(address, set()))
        self.assertIn("local user or host identifier", checker.scan(address + b":local-person/repo.git", {b"local-person"}))
        token = b"ghp_" + b"A" * 36
        self.assertIn("possible access token", checker.scan(address + b":owner/" + token, set()))

    def test_only_explicit_home_username_placeholders_are_exempt(self):
        prefix = b"/" + b"Users/"
        for placeholder in (b"...", "…".encode()):
            self.assertEqual(checker.scan(prefix + placeholder + b"/file", set()), set())
            self.assertIn("absolute home directory", checker.scan(prefix + b"real/" + placeholder + b"/file", set()))
            self.assertIn("absolute home directory", checker.scan(prefix + placeholder + b"real/file", set()))
        path = prefix + b".../" + b"ghp_" + b"A" * 36
        self.assertIn("possible access token", checker.scan(path, set()))


if __name__ == "__main__":
    unittest.main()
