from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from updater import (PreparedUpdate, Release, REQUIRED_FILES, UpdateCancelled,
                     UpdateError, Updater, validate_package, version_tuple)
from update_worker import UpdateLock, install_package, process_identity, recover_install, restart_app, verify_and_restart


def make_package(path: Path, version="1.1.0"):
    for relative in REQUIRED_FILES:
        file = path / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"MZfixture" if relative.endswith(".exe") else b"fixture")
    (path / "app/version.py").write_text(f'VERSION = "{version}"\n', encoding="utf-8")
    (path / "app/update-channel.json").write_text(json.dumps({"schema": 1,
        "repository": "example/sms-cleaner", "public_key": base64.b64encode(bytes(32)).decode()}), encoding="utf-8")
    (path / "README.md").write_text("new README", encoding="utf-8")


class OwnedFakeProcess:
    pid = 123456
    def __init__(self):
        self.returncode = None
        self.terminate_calls = 0
        self.kill_calls = 0
    def poll(self):
        return self.returncode
    def terminate(self):
        self.terminate_calls += 1
        self.returncode = -15
    def kill(self):
        self.kill_calls += 1
        self.returncode = -9
    def wait(self, timeout=None):
        return self.returncode


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.install = self.root / "installed"
        self.install.mkdir()
        self.resources = self.root / "resources"
        self.resources.mkdir()
        self.private = Ed25519PrivateKey.generate()
        public = self.private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        (self.resources / "update-channel.json").write_text(json.dumps({
            "schema": 1, "repository": "example/sms-cleaner", "public_key": base64.b64encode(public).decode()}))
        self.events = []
        self.updater = Updater(self.install, self.resources, self.root / "updates", progress=self.events.append)

    def tearDown(self):
        self.temporary.cleanup()

    def release_data(self, size=10, digest="a" * 64):
        version = "1.2.0"
        tag = "v" + version
        prefix = "https://github.com/example/sms-cleaner/releases/download/" + tag + "/"
        asset = "sms-batch-cleaner-1.2.0-win-x64.zip"
        manifest = {"schema": 1, "repository": "example/sms-cleaner", "version": version,
                    "tag": tag, "min_updater_version": "1.1.0", "asset": asset, "size": size, "sha256": digest}
        raw = json.dumps(manifest).encode()
        envelope = {"payload": base64.b64encode(raw).decode(),
                    "signature": base64.b64encode(self.private.sign(raw)).decode()}
        api = {"tag_name": tag, "prerelease": False, "draft": False, "body": "Changes",
               "assets": [{"name": "update.json", "browser_download_url": prefix + "update.json"},
                          {"name": asset, "browser_download_url": prefix + asset, "size": size}]}
        return api, envelope

    def test_versions(self):
        self.assertGreater(version_tuple("1.10.0"), version_tuple("1.9.9"))
        for bad in ("1.0", "v1.0.0", "1.0.0-beta", "01.2.3", None):
            with self.subTest(bad=bad), self.assertRaises(UpdateError):
                version_tuple(bad)

    def test_signed_check_and_old_app_version(self):
        api, manifest = self.release_data()
        with patch.object(self.updater, "_json", side_effect=[api, manifest]):
            release = self.updater.check("1.0.0")
        self.assertEqual(release.version, "1.2.0")
        with patch.object(self.updater, "_json", return_value=api):
            self.assertIsNone(self.updater.check("1.2.0"))

    def test_reject_unsigned_and_bad_signature(self):
        api, envelope = self.release_data()
        for bad in ({"schema": 1}, {**envelope, "signature": base64.b64encode(b"x" * 64).decode()}):
            with patch.object(self.updater, "_json", side_effect=[api, bad]), self.assertRaises(UpdateError):
                self.updater.check("1.0.0")

    def test_reject_wrong_repository_asset(self):
        api, envelope = self.release_data()
        api["assets"][1]["browser_download_url"] = "https://github.com/other/repo/releases/download/v1.2.0/a.zip"
        with patch.object(self.updater, "_json", side_effect=[api, envelope]), self.assertRaises(UpdateError):
            self.updater.check("1.0.0")

    def test_sha_mismatch(self):
        archive = self.root / "package.zip"
        archive.write_bytes(b"damaged")
        with self.assertRaises(UpdateError):
            self.updater._verify(archive, "0" * 64)

    def test_zip_path_rejections(self):
        for name in ("../evil", "SmsCleaner/../evil", "C:/evil", "SmsCleaner/app/a:b",
                     "SmsCleaner\\app\\evil", "SmsCleaner/data/messages.json", "SmsCleaner/app/NUL.txt",
                     "SmsCleaner/app/trailing."):
            with self.subTest(name=name):
                archive = self.root / "invalid.zip"
                with zipfile.ZipFile(archive, "w") as zipped:
                    zipped.writestr(name, b"unsafe")
                if "\\" in name:
                    # ZipInfo normalizes native Windows separators when writing;
                    # emulate a hostile archive produced on another platform.
                    archive.write_bytes(archive.read_bytes().replace(name.replace("\\", "/").encode(), name.encode()))
                stage = self.root / "extract"
                stage.mkdir(exist_ok=True)
                with self.assertRaises(UpdateError):
                    self.updater._extract(archive, stage)

    def test_zip_case_collision_and_symlink(self):
        archive = self.root / "invalid.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("SmsCleaner/runtime/Lib/a.py", b"a")
            zipped.writestr("SmsCleaner/runtime/lib/b.py", b"b")
        with self.assertRaises(UpdateError):
            self.updater._extract(archive, self.root / "out")
        with zipfile.ZipFile(archive, "w") as zipped:
            entry = zipfile.ZipInfo("SmsCleaner/app/link")
            entry.create_system = 3
            entry.external_attr = 0o120777 << 16
            zipped.writestr(entry, "../../data")
        with self.assertRaises(UpdateError):
            self.updater._extract(archive, self.root / "out")

    def test_download_full_package_and_progress(self):
        package = self.root / "package"
        make_package(package, "1.2.0")
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as zipped:
            for file in package.rglob("*"):
                if file.is_file():
                    zipped.write(file, "SmsCleaner/" + file.relative_to(package).as_posix())
        data = stream.getvalue()
        api, envelope = self.release_data(len(data), hashlib.sha256(data).hexdigest())
        with patch.object(self.updater, "_json", side_effect=[api, envelope]):
            release = self.updater.check("1.0.0")
        with patch.object(self.updater, "_open", return_value=io.BytesIO(data)):
            prepared = self.updater.download(release)
        validate_package(prepared.package_dir, "1.2.0")
        self.assertEqual({x["phase"] for x in self.events}, {"download", "verify", "extract"})
        self.updater.verify_prepared(prepared)
        (prepared.package_dir / "app/core.py").write_text("tampered", encoding="utf-8")
        with self.assertRaises(UpdateError):
            self.updater.verify_prepared(prepared)

    def test_cancel_stops_verification(self):
        file = self.root / "file"
        file.write_bytes(b"data")
        self.updater.cancel()
        with self.assertRaises(UpdateCancelled):
            self.updater._verify(file, hashlib.sha256(b"data").hexdigest())

    def setup_installer(self):
        make_package(self.install, "1.0.0")
        data = self.install / "data"
        data.mkdir()
        (data / "private.json").write_bytes(b"must stay exactly unchanged")
        package = self.root / "staged/SmsCleaner"
        make_package(package, "1.1.0")
        return package

    def assert_old_intact(self):
        validate_package(self.install, "1.0.0")
        self.assertEqual((self.install / "data/private.json").read_bytes(), b"must stay exactly unchanged")
        self.assertFalse((self.install / ".update-journal.json").exists())

    def test_install_preserves_data_and_stage(self):
        package = self.setup_installer()
        checked = []
        install_package(self.install, package, "1.1.0", verify=lambda p: checked.append(p))
        validate_package(self.install, "1.1.0")
        validate_package(package, "1.1.0")
        self.assertEqual(checked, [self.install])
        self.assertEqual((self.install / "data/private.json").read_bytes(), b"must stay exactly unchanged")

    def test_copy_failure_never_replaces_current(self):
        package = self.setup_installer()
        def broken_copy(source, destination):
            raise OSError("simulated disk full")
        with self.assertRaises(OSError):
            install_package(self.install, package, "1.1.0", copy_file=broken_copy, verify=lambda p: None)
        self.assert_old_intact()

    def test_mid_rename_failure_rolls_back(self):
        package = self.setup_installer()
        calls = []
        def broken_rename(source, destination):
            calls.append((source, destination))
            if len(calls) == 4:
                raise OSError("simulated file lock")
            os.replace(source, destination)
        with self.assertRaises(OSError):
            install_package(self.install, package, "1.1.0", rename=broken_rename, verify=lambda p: None)
        self.assert_old_intact()

    def test_smoke_failure_rolls_back_all_files(self):
        package = self.setup_installer()
        (self.install / "README.md").write_text("old README", encoding="utf-8")
        def failed_smoke(path):
            raise UpdateError("new runtime failed")
        with self.assertRaises(UpdateError):
            install_package(self.install, package, "1.1.0", verify=failed_smoke)
        self.assert_old_intact()
        self.assertEqual((self.install / "README.md").read_text(encoding="utf-8"), "old README")

    def test_lock_and_process_identity(self):
        with UpdateLock(self.install), self.assertRaises(UpdateError):
            with UpdateLock(self.install):
                pass
        self.assertIsNotNone(process_identity(os.getpid()))

    def test_crash_journal_restores_partial_replacement(self):
        package = self.setup_installer()
        workspace = self.install.parent / ("." + self.install.name + ".update-crashfixture")
        (workspace / "old").mkdir(parents=True)
        (workspace / "new").mkdir()
        os.replace(self.install / "app", workspace / "old/app")
        shutil.copytree(package / "app", self.install / "app")
        (self.install / "CHANGELOG.md").write_text("new-only-file", encoding="utf-8")
        journal = {"schema": 1, "workspace": workspace.name, "phase": "replacing", "items": [
            {"name": "app", "old_exists": True, "started": True},
            {"name": "runtime", "old_exists": True, "started": False},
            {"name": "CHANGELOG.md", "old_exists": False, "started": True}]}
        (self.install / ".update-journal.json").write_text(json.dumps(journal), encoding="utf-8")
        with UpdateLock(self.install):
            self.assertTrue(recover_install(self.install))
        self.assert_old_intact()
        self.assertFalse((self.install / "CHANGELOG.md").exists())

    def test_restart_requires_ready_version_and_pid(self):
        self.setup_installer()
        def ready_process(command, **kwargs):
            report = Path(command[command.index("--startup-report") + 1])
            report.write_text(json.dumps({"ready": True, "version": "1.1.0", "pid": OwnedFakeProcess.pid}), encoding="utf-8")
            return OwnedFakeProcess()
        with patch("update_worker.subprocess.Popen", side_effect=ready_process):
            running = restart_app(self.install, "1.1.0")
        self.assertEqual(running.terminate_calls, 0)
        failed = OwnedFakeProcess()
        with patch("update_worker.subprocess.Popen", return_value=failed), \
                patch("update_worker.time.monotonic", side_effect=[0, 16]), self.assertRaises(UpdateError):
            restart_app(self.install, "1.1.0")
        self.assertEqual(failed.terminate_calls, 1)

    def test_normal_window_readiness_failure_stops_child_and_rolls_back(self):
        package = self.setup_installer()
        failed = OwnedFakeProcess()
        with patch("update_worker.smoke_test", return_value=None), \
                patch("update_worker.subprocess.Popen", return_value=failed), \
                patch("update_worker.time.monotonic", side_effect=[0, 16]), self.assertRaises(UpdateError):
            install_package(self.install, package, "1.1.0",
                            verify=lambda path: verify_and_restart(path, "1.1.0"))
        self.assertEqual(failed.terminate_calls, 1)
        self.assert_old_intact()

    def test_commit_failure_stops_only_new_child_and_rolls_back(self):
        import update_worker
        package = self.setup_installer()
        child = OwnedFakeProcess()
        original_write = update_worker._write_json
        def failed_commit(path, value):
            if value.get("phase") == "complete":
                raise OSError("simulated commit disk error")
            return original_write(path, value)
        with patch("update_worker._write_json", side_effect=failed_commit), self.assertRaises(OSError):
            install_package(self.install, package, "1.1.0", verify=lambda path: child)
        self.assertEqual(child.terminate_calls, 1)
        self.assert_old_intact()


if __name__ == "__main__":
    unittest.main()
