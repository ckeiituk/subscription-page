"""Offline deployment checks, including rollback and fail-closed CI selection."""
import importlib.util
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("deployer", Path(__file__).with_name("deploy-x-out.py"))
deployer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployer)

COMPOSE = b"""services:
  remnawave-subscription-page:
    image: out/subscription-page:old
    pull_policy: never
    env_file: .env
    volumes:
      - ./index.html:/opt/app/frontend/index.html:ro
  unrelated:
    image: keep-me:1
networks:
  shared:
    external: true
"""
PROBE = {"status": 200, "groups": 3, "xOutApplied": True}


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.compose = self.root / "docker-compose.yml"
        self.compose.write_bytes(COMPOSE)
        self.state = self.root / "state"
        self.state.mkdir()

    def tearDown(self):
        self.directory.cleanup()

    def test_image_change_preserves_other_services_mounts_and_environment(self):
        updated = deployer.replace_image(COMPOSE, "out/subscription-page:new")
        self.assertEqual(updated.replace(b"out/subscription-page:new", b"out/subscription-page:old"), COMPOSE)
        for invalid in [COMPOSE.replace(b"    image: out/subscription-page:old\n", b""),
                        COMPOSE.replace(b"  remnawave-subscription-page:", b"  other:")]:
            with self.assertRaises(ValueError):
                deployer.replace_image(invalid, "new")

    def test_failed_live_probe_restores_original_compose_and_exact_previous_image(self):
        overrides = []

        def up(directory, override=None):
            if override:
                self.assertEqual(self.compose.read_bytes(), COMPOSE)
                overrides.append(json.loads(override.read_text()))

        with patch.object(deployer, "inspect", return_value={"Image": "sha256:previous"}), \
             patch.object(deployer, "run"), patch.object(deployer, "compose_up", side_effect=up), \
             patch.object(deployer, "live_probe", side_effect=[PROBE, RuntimeError("bad response"), PROBE]):
            with self.assertRaisesRegex(RuntimeError, "bad response"):
                deployer.activate(self.root, self.state, "new")
        self.assertEqual(self.compose.read_bytes(), COMPOSE)
        self.assertEqual(overrides[0]["services"][deployer.SERVICE]["image"], "sha256:previous")
        self.assertEqual(next(self.state.glob("rollback-*/docker-compose.yml")).read_bytes(), COMPOSE)

    def test_compose_start_failure_also_rolls_back(self):
        with patch.object(deployer, "inspect", return_value={"Image": "sha256:previous"}), \
             patch.object(deployer, "run"), \
             patch.object(deployer, "compose_up", side_effect=[RuntimeError("unhealthy"), None]) as up, \
             patch.object(deployer, "live_probe", return_value=PROBE):
            with self.assertRaisesRegex(RuntimeError, "unhealthy"):
                deployer.activate(self.root, self.state, "new")
        self.assertEqual(up.call_count, 2)
        self.assertEqual(self.compose.read_bytes(), COMPOSE)

    def test_lost_x_out_processing_triggers_rollback(self):
        with patch.object(deployer, "inspect", return_value={"Image": "sha256:previous"}), \
             patch.object(deployer, "run"), patch.object(deployer, "compose_up"), \
             patch.object(deployer, "live_probe", side_effect=[PROBE, {**PROBE, "xOutApplied": False}, PROBE]):
            with self.assertRaisesRegex(RuntimeError, "lost x-out"):
                deployer.activate(self.root, self.state, "new")
        self.assertEqual(self.compose.read_bytes(), COMPOSE)

    def test_concurrent_edit_is_preserved_instead_of_overwritten_by_rollback(self):
        edited = COMPOSE + b"# changed by operator\n"

        def up(*args):
            self.compose.write_bytes(edited)
            raise RuntimeError("bad start")

        with patch.object(deployer, "inspect", return_value={"Image": "sha256:previous"}), \
             patch.object(deployer, "run"), patch.object(deployer, "compose_up", side_effect=up), \
             patch.object(deployer, "live_probe", return_value=PROBE):
            with self.assertRaisesRegex(RuntimeError, "concurrently"):
                deployer.activate(self.root, self.state, "new")
        self.assertEqual(self.compose.read_bytes(), edited)

    def test_success_keeps_new_image_and_original_config_backup(self):
        with patch.object(deployer, "inspect", return_value={"Image": "sha256:previous"}), \
             patch.object(deployer, "run"), patch.object(deployer, "compose_up") as up, \
             patch.object(deployer, "live_probe", return_value=PROBE):
            deployer.activate(self.root, self.state, "new")
            up.assert_called_once_with(self.root)
        self.assertIn(b"image: new\n", self.compose.read_bytes())
        self.assertEqual(next(self.state.glob("rollback-*/docker-compose.yml")).read_bytes(), COMPOSE)

    def test_unchecked_commit_does_not_build_or_activate(self):
        def command(*args, **kwargs):
            if "get-url" in args:
                return f"https://github.com/{deployer.REPOSITORY}.git"
            if "rev-parse" in args:
                return "a" * 40
            return ""

        with patch.object(deployer, "run", side_effect=command), \
             patch.object(deployer, "ci_passed", return_value=False), \
             patch.object(deployer.subprocess, "run") as process, \
             patch.object(deployer, "activate") as activate:
            deployer.deploy(self.root, self.root, self.state)
            process.assert_not_called()
            activate.assert_not_called()
        self.assertEqual(self.compose.read_bytes(), COMPOSE)

    def test_failed_revision_is_not_retried_automatically(self):
        (self.state / "state.json").write_text(json.dumps({"failed": "a" * 40}))

        def command(*args, **kwargs):
            if "get-url" in args:
                return f"https://github.com/{deployer.REPOSITORY}.git"
            return "a" * 40 if "rev-parse" in args else ""

        with patch.object(deployer, "run", side_effect=command), \
             patch.object(deployer, "ci_passed") as ci:
            with self.assertRaisesRegex(RuntimeError, "failed previously"):
                deployer.deploy(self.root, self.root, self.state)
            ci.assert_not_called()


class RetentionTests(unittest.TestCase):
    def test_only_obsolete_updater_images_are_removed(self):
        removed = []
        tags = ["out/subscription-page:git-current", "out/subscription-page:git-previous",
                "out/subscription-page:git-obsolete"]
        def run(*args, **kwargs):
            if "ls" in args:
                self.assertIn("reference=out/subscription-page:git-*", args)
                return "\n".join(tags)
            if "inspect" in args:
                return "sha256:" + args[-1].split("git-")[1]
            if "rm" in args:
                self.assertNotIn("--force", args)
                removed.append(args[-1])
            return ""
        with patch.object(deployer, "run", side_effect=run), \
             patch.object(deployer, "inspect", return_value={"Image": "sha256:current"}):
            deployer.clean_images("sha256:previous")
        self.assertEqual(removed, [tags[-1]])


class CISelectionTests(unittest.TestCase):
    def setUp(self):
        self.run = {"id": 10, "head_sha": "a" * 40, "head_branch": deployer.BRANCH,
                    "head_repository": {"full_name": deployer.REPOSITORY},
                    "path": ".github/workflows/x-out-check.yml", "event": "push",
                    "status": "completed", "conclusion": "success"}

    def test_only_correct_repo_branch_workflow_event_and_sha_qualify(self):
        self.assertEqual(deployer.latest_check([self.run], "a" * 40), self.run)
        for key, value in [("head_sha", "b" * 40), ("head_branch", "other"),
                           ("head_repository", {"full_name": "other/repo"}),
                           ("path", ".github/workflows/other.yml"), ("event", "pull_request")]:
            changed = {**self.run, key: value}
            self.assertIsNone(deployer.latest_check([changed], "a" * 40))

    def test_latest_failure_wins_over_previous_success(self):
        newer = {**self.run, "id": 11, "conclusion": "failure"}
        self.assertEqual(deployer.latest_check([self.run, newer], "a" * 40), newer)


class ImageDownloadTests(unittest.TestCase):
    def setUp(self):
        self.sha = "a" * 40
        self.archive = b"synthetic image archive"
        self.checksum = hashlib.sha256(self.archive).hexdigest()
        self.url = f"https://github.com/{deployer.REPOSITORY}/releases/download/x-out-{self.sha}/"
        self.release = {"tag_name": f"x-out-{self.sha}", "target_commitish": self.sha,
                        "draft": False, "author": {"login": "github-actions[bot]"},
                        "assets": [{"name": name, "browser_download_url": self.url + name,
                                    "size": len(self.archive)}
                                   for name in ("image.tar.gz", "image.tar.gz.sha256")]}

    def responses(self, archive=None):
        return [io.BytesIO(json.dumps(self.release).encode()),
                io.BytesIO(f"{self.checksum}  image.tar.gz\n".encode()),
                io.BytesIO(self.archive if archive is None else archive)]

    def test_verified_image_is_loaded_and_revision_checked(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(deployer.urllib.request, "urlopen", side_effect=self.responses()), \
             patch.object(deployer, "run", side_effect=["", self.sha]) as run:
            image = deployer.download_image(self.sha, Path(directory))
            self.assertEqual(image, f"out/subscription-page:git-{self.sha}")
            self.assertEqual(run.call_args_list[0].args[:2], ("docker", "load"))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_corrupt_archive_is_never_loaded(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(deployer.urllib.request, "urlopen", side_effect=self.responses(b"corrupt")), \
             patch.object(deployer, "run") as run:
            with self.assertRaisesRegex(ValueError, "checksum/size mismatch"):
                deployer.download_image(self.sha, Path(directory))
            run.assert_not_called()

    def test_wrong_commit_or_publisher_is_rejected_before_download(self):
        for key, value in [("target_commitish", "b" * 40), ("draft", True),
                           ("author", {"login": "untrusted"})]:
            release = {**self.release, key: value}
            with tempfile.TemporaryDirectory() as directory, \
                 patch.object(deployer.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(release).encode())) as read, \
                 patch.object(deployer, "run") as run:
                with self.assertRaisesRegex(ValueError, "tested commit and publisher"):
                    deployer.download_image(self.sha, Path(directory))
                self.assertEqual(read.call_count, 1)
                run.assert_not_called()

    def test_wrong_loaded_revision_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(deployer.urllib.request, "urlopen", side_effect=self.responses()), \
             patch.object(deployer, "run", side_effect=["", "b" * 40]):
            with self.assertRaisesRegex(ValueError, "revision differs"):
                deployer.download_image(self.sha, Path(directory))


if __name__ == "__main__":
    unittest.main()
