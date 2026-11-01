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
                 patch.object(updater, "keep_schedule_alive"), \
                 patch.object(updater, "ensure_checks") as checks, \
                 patch.object(updater, "run") as run:
                updater.main()
                run.assert_not_called()
                checks.assert_called_once_with("ckeiituk/subscription-page", "out/x-out")

    def test_closed_pr_is_not_reopened(self):
        with patch.dict(os.environ, UPDATE_REPOSITORY="ckeiituk/subscription-page", UPDATE_BASE_BRANCH="out/x-out"), \
             patch.object(Path, "read_text", return_value=DOCKERFILE), \
             patch.object(updater, "keep_schedule_alive"), \
             patch.object(updater, "output", side_effect=[
                 "abc\trefs/tags/8.1.0",
                 json.dumps([{"number": 1, "state": "CLOSED", "url": "https://example.org/pr/1"}]),
             ]), patch.object(updater, "run") as run:
            updater.main()
            run.assert_not_called()

    def test_checks_dispatch_once(self):
        with patch.object(updater, "check_runs", return_value=[]), patch.object(updater, "run") as run:
            updater.ensure_checks("ckeiituk/subscription-page", "out/upstream-8.1.0", "a" * 40)
            self.assertIn("--ref", run.call_args.args)
        result = {"databaseId": 1, "headSha": "a" * 40, "event": "workflow_dispatch"}
        with patch.object(updater, "check_runs", return_value=[result]), patch.object(updater, "run") as run:
            self.assertEqual(updater.ensure_checks("ckeiituk/subscription-page", "out/upstream-8.1.0", "a" * 40), result)
            run.assert_not_called()

    def test_old_commit_and_pull_request_runs_do_not_satisfy_head_check(self):
        results = [{"headSha": "b" * 40, "event": "workflow_dispatch"},
                   {"headSha": "a" * 40, "event": "pull_request"}]
        with patch.object(updater, "check_runs", return_value=results), patch.object(updater, "run") as run:
            updater.ensure_checks("ckeiituk/subscription-page", "out/upstream-8.1.0", "a" * 40)
            self.assertEqual(run.call_args.args[:3], ("gh", "workflow", "run"))

    def test_newer_failed_run_is_not_hidden_by_old_success(self):
        results = [{"headSha": "a" * 40, "event": "workflow_dispatch", "conclusion": status}
                   for status in ("failure", "success")]
        with patch.object(updater, "check_runs", return_value=results), patch.object(updater, "run") as run:
            result = updater.ensure_checks("ckeiituk/subscription-page", "out/upstream-8.1.0", "a" * 40)
            self.assertEqual(result["conclusion"], "failure")
            run.assert_not_called()


class HeartbeatTests(unittest.TestCase):
    def test_active_repository_does_not_create_heartbeat(self):
        with patch.object(updater.time, "time", return_value=100), \
             patch.object(updater, "output", return_value="50"), patch.object(updater, "run") as run:
            updater.keep_schedule_alive("ckeiituk/subscription-page", "out/x-out")
            run.assert_not_called()

    def test_inactive_repository_gets_a_checked_heartbeat_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            os.chdir(directory)
            Path(".github").mkdir()
            try:
                with patch.object(updater.time, "time", return_value=31 * 86400), \
                     patch.object(updater, "output", side_effect=["0", ""]), \
                     patch.object(updater, "run") as run, patch.object(updater, "ensure_checks") as checks:
                    updater.keep_schedule_alive("ckeiituk/subscription-page", "out/x-out")
                    self.assertTrue(Path(".github/x-out-updater-heartbeat").read_text().strip())
                    self.assertTrue(any(c.args[:2] == ("git", "push") for c in run.call_args_list))
                    checks.assert_called_once_with("ckeiituk/subscription-page", "out/x-out")
            finally:
                os.chdir(previous)


class AutoMergeTests(unittest.TestCase):
    def setUp(self):
        self.pr = {"state": "OPEN", "isDraft": False, "isCrossRepository": False,
                   "author": {"login": "app/github-actions"},
                   "headRefName": "out/upstream-8.1.0", "baseRefName": "out/x-out",
                   "headRefOid": "a" * 40, "baseRefOid": "b" * 40}

    def exercise(self, result=None, changes=None, behind=False, race=False):
        if result is None:
            result = {"status": "completed", "conclusion": "success", "databaseId": 42}

        def output(*args):
            if args[:3] == ("gh", "pr", "view"):
                return json.dumps(self.pr)
            return ("c" * 40 if race else self.pr["baseRefOid"]) if args[-1].endswith("base") else self.pr["headRefOid"]

        def run(*args, **kwargs):
            return subprocess.CompletedProcess(args, 1 if behind and "merge-base" in args else 0, "", "")

        with patch.object(updater, "output", side_effect=output), \
             patch.object(updater, "run", side_effect=run) as commands, \
             patch.object(updater, "protected_changes", return_value=changes or []), \
             patch.object(updater, "ensure_checks", return_value=result) as checks:
            updater.auto_merge_pr("ckeiituk/subscription-page", "out/x-out", 1, "8.1.0")
            return [call.args for call in commands.call_args_list], checks

    def test_success_merges_the_tested_head_and_checks_merged_base(self):
        commands, checks = self.exercise()
        merges = [cmd for cmd in commands if cmd[:3] == ("gh", "pr", "merge")]
        self.assertEqual(len(merges), 1)
        self.assertEqual(merges[0][-2:], ("--match-head-commit", self.pr["headRefOid"]))
        self.assertEqual(checks.call_args.args, ("ckeiituk/subscription-page", "out/x-out"))

    def test_untrusted_pr_is_never_merged_or_checked(self):
        for field, value in [("isDraft", True), ("isCrossRepository", True),
                             ("author", {"login": "someone"}), ("headRefName", "other"),
                             ("baseRefName", "main")]:
            original = self.pr.copy()
            self.pr[field] = value
            commands, checks = self.exercise()
            self.assertEqual(commands, [])
            checks.assert_not_called()
            self.pr = original

    def test_failed_or_pending_checks_cannot_merge(self):
        for status, conclusion in [("in_progress", None), ("completed", "failure"),
                                   ("completed", "cancelled"), ("completed", "skipped")]:
            commands, _ = self.exercise({"status": status, "conclusion": conclusion})
            self.assertFalse(any(cmd[:3] == ("gh", "pr", "merge") for cmd in commands))

    def test_protected_changes_require_manual_review(self):
        commands, checks = self.exercise(changes=["scripts/deploy-x-out.py"])
        self.assertFalse(any(cmd[:3] == ("gh", "pr", "merge") for cmd in commands))
        checks.assert_not_called()

    def test_stale_base_is_updated_but_never_merged(self):
        commands, _ = self.exercise(behind=True)
        self.assertTrue(any("repos/ckeiituk/subscription-page/pulls/1/update-branch" in cmd for cmd in commands))
        self.assertFalse(any(cmd[:3] == ("gh", "pr", "merge") for cmd in commands))

    def test_concurrent_ref_change_stops_merge(self):
        with self.assertRaisesRegex(RuntimeError, "changed during inspection"):
            self.exercise(race=True)


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

    def test_protected_changes_allow_only_the_image_pin(self):
        before = self.git("rev-parse", "HEAD")
        Path("Dockerfile.x-out").write_text(updater.pin_image(DOCKERFILE, "8.1.0", DIGEST))
        self.commit("version only")
        self.assertEqual(updater.protected_changes(before, "HEAD"), [])
        Path("Dockerfile.x-out").write_text(Path("Dockerfile.x-out").read_text() + "RUN echo unexpected\n")
        Path("scripts").mkdir()
        Path("scripts/deploy-x-out.py").write_text("changed deployment\n")
        self.commit("change policy")
        self.assertEqual(set(updater.protected_changes(before, "HEAD")),
                         {"Dockerfile.x-out", "scripts/deploy-x-out.py"})


if __name__ == "__main__":
    unittest.main()
