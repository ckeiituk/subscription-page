"""Offline checks for updater decisions and real clean/conflicting Git merges."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("updater", Path(__file__).with_name("update-upstream.py"))
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)
DIGEST = "sha256:" + "a" * 64
DOCKERFILE = (
    f"ARG UPSTREAM_IMAGE=remnawave/subscription-page:8.0.0@{DIGEST}\n"
    'LABEL io.out.upstream-version="8.0.0"\n'
)


class UpdaterTests(unittest.TestCase):
    def test_selects_highest_stable_numeric_tag(self):
        refs = "\n".join(f"abc\trefs/tags/{tag}" for tag in ["8.9.0", "8.10.0", "9.0.0-beta", "dev", "7.0.0"])
        self.assertEqual(updater.latest_stable_tag(refs), "8.10.0")
        with self.assertRaises(ValueError):
            updater.latest_stable_tag("abc\trefs/tags/dev")

    def test_versions_reject_untrusted_or_prerelease_tags(self):
        self.assertGreater(updater.version("v8.10.0"), updater.version("8.9.1"))
        for invalid in ["8.1.0-beta", "main", "8.1.0;echo secret", "../8.1.0"]:
            with self.assertRaises(ValueError):
                updater.version(invalid)

    def test_pin_updates_both_version_and_digest(self):
        result = updater.pin_image(DOCKERFILE, "8.1.0", "sha256:" + "b" * 64)
        self.assertEqual(updater.current_version(result), "8.1.0")
        self.assertIn('io.out.upstream-version="8.1.0"', result)
        self.assertIn("@sha256:" + "b" * 64, result)
        with self.assertRaises(ValueError):
            updater.pin_image(DOCKERFILE, "8.1.0", "latest")

    def test_same_or_older_release_does_not_write(self):
        for tag in ["8.0.0", "7.9.0"]:
            with patch.dict(os.environ, UPDATE_REPOSITORY="ckeiituk/subscription-page", UPDATE_BASE_BRANCH="out/x-out"), \
                 patch.object(Path, "read_text", return_value=DOCKERFILE), \
                 patch.object(updater, "output", return_value=f"abc\trefs/tags/{tag}"), \
                 patch.object(updater, "run") as run:
                updater.main()
                run.assert_not_called()

    def test_closed_pr_is_not_reopened(self):
        with patch.dict(os.environ, UPDATE_REPOSITORY="ckeiituk/subscription-page", UPDATE_BASE_BRANCH="out/x-out"), \
             patch.object(Path, "read_text", return_value=DOCKERFILE), \
             patch.object(updater, "output", side_effect=[
                 "abc\trefs/tags/8.1.0",
                 json.dumps([{"number": 1, "state": "CLOSED", "url": "https://example.org/pr/1"}]),
             ]), patch.object(updater, "run") as run:
            updater.main()
            run.assert_not_called()

    def test_checks_dispatch_once(self):
        with patch.object(updater, "output", return_value="[]"), patch.object(updater, "run") as run:
            updater.ensure_checks("ckeiituk/subscription-page", "out/upstream-8.1.0")
            self.assertIn("--ref", run.call_args.args)
        with patch.object(updater, "output", return_value='[{"databaseId": 1}]'), patch.object(updater, "run") as run:
            updater.ensure_checks("ckeiituk/subscription-page", "out/upstream-8.1.0")
            run.assert_not_called()


class GitMergeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous = Path.cwd()
        os.chdir(self.directory.name)
        self.git("init", "-b", "base")
        self.git("config", "user.name", "Offline test")
        self.git("config", "user.email", "test@example.invalid")
        Path("shared.txt").write_text("original\n")
        self.commit("initial")
        self.git("switch", "-c", "release")
        Path("shared.txt").write_text("new upstream\n")
        self.commit("upstream")
        self.git("update-ref", "refs/x-out/upstream-release", "HEAD")
        self.git("switch", "base")
        Path("Dockerfile.x-out").write_text(DOCKERFILE)
        Path("our-extension.txt").write_text("preserve me\n")
        self.commit("our extension")

    def tearDown(self):
        os.chdir(self.previous)
        self.directory.cleanup()

    def git(self, *args):
        return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "-m", message)

    def test_clean_merge_preserves_extension_and_release_ancestry(self):
        conflicts = updater.prepare_branch("8.1.0", DIGEST, "out/upstream-8.1.0")
        self.assertEqual(conflicts, [])
        self.assertEqual(Path("our-extension.txt").read_text(), "preserve me\n")
        self.assertEqual(Path("shared.txt").read_text(), "new upstream\n")
        self.assertEqual(updater.current_version(Path("Dockerfile.x-out").read_text()), "8.1.0")
        self.git("merge-base", "--is-ancestor", "refs/x-out/upstream-release", "HEAD")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_conflict_aborts_merge_and_records_files(self):
        Path("shared.txt").write_text("our changes\n")
        self.commit("conflict")
        conflicts = updater.prepare_branch("8.1.0", DIGEST, "out/upstream-8.1.0")
        self.assertEqual(conflicts, ["shared.txt"])
        self.assertEqual(Path("shared.txt").read_text(), "our changes\n")
        self.assertEqual(Path("Dockerfile.x-out").read_text(), DOCKERFILE)
        self.assertIn("shared.txt", updater.CONFLICT_REPORT.read_text())
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_dirty_checkout_is_rejected(self):
        Path("our-extension.txt").write_text("unfinished edit\n")
        with self.assertRaisesRegex(ValueError, "clean checkout"):
            updater.prepare_branch("8.1.0", DIGEST, "out/upstream-8.1.0")
        self.assertEqual(self.git("branch", "--show-current"), "base")


if __name__ == "__main__":
    unittest.main()
