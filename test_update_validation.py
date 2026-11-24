"""Update handoff and channel validation tests without launching an installer."""

import base64
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from updater import PreparedUpdate, REQUIRED_FILES, UpdateError, Updater, validate_package


class UpdateValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sms-cleaner-update-validation-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stage = self.root / "stage"
        self.package = self.stage / "SmsCleaner"
        self.package.mkdir(parents=True)
        for name in REQUIRED_FILES:
            path = self.package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"MZfixture" if name.endswith(".exe") else b"fixture")
        (self.package / "app/version.py").write_text('VERSION = "1.1.0"\n', encoding="utf-8")
        self.channel_path = self.package / "app/update-channel.json"
        self.channel = {"schema": 1, "repository": "example/sms-cleaner",
                        "public_key": base64.b64encode(b"x" * 32).decode("ascii")}
        self.write_channel(self.channel)
        self.updater = Updater(self.root / "installed", self.root / "resources", self.root / "data")
        self.prepared = PreparedUpdate(self.stage, self.package, "1.1.0")

    def write_channel(self, value):
        self.channel_path.write_text(json.dumps(value), encoding="utf-8")

    def test_valid_package_requires_channel_configuration(self):
        validate_package(self.package, "1.1.0")
        self.channel_path.unlink()
        with self.assertRaises(UpdateError) as caught:
            validate_package(self.package, "1.1.0")
        self.assertIn("app/update-channel.json", str(caught.exception))

    def test_invalid_channel_schema_and_repository_are_rejected(self):
        invalid = [[], None, {**self.channel, "schema": True}, {**self.channel, "schema": 2},
                   {**self.channel, "repository": "https://example.com/repo"},
                   {**self.channel, "repository": "../elsewhere"},
                   {**self.channel, "repository": None}]
        for channel in invalid:
            with self.subTest(channel=channel):
                self.write_channel(channel)
                with self.assertRaises(UpdateError):
                    validate_package(self.package, "1.1.0")

    def test_public_key_must_be_valid_base64_of_exactly_32_bytes(self):
        keys = [None, "not base64!", base64.b64encode(b"x" * 31).decode(),
                base64.b64encode(b"x" * 33).decode(), "", 32]
        for key in keys:
            with self.subTest(key=key):
                self.write_channel({**self.channel, "public_key": key})
                with self.assertRaises(UpdateError):
                    validate_package(self.package, "1.1.0")

    def test_malformed_channel_is_not_executed_or_echoed(self):
        secret = "PRIVATE_CHANNEL_CONTENT"
        self.channel_path.write_text("raise RuntimeError('" + secret + "')", encoding="utf-8")
        with self.assertRaises(UpdateError) as caught:
            validate_package(self.package, "1.1.0")
        self.assertNotIn(secret, str(caught.exception))

    def launch_with_report(self, report, exit_code=None):
        process = SimpleNamespace(pid=4321, poll=Mock(return_value=exit_code))

        def start(command, **kwargs):
            ready = Path(command[command.index("--ready-file") + 1])
            ready.write_text(json.dumps(report), encoding="utf-8")
            return process

        with patch.object(self.updater, "verify_prepared"), patch("update_worker.process_identity", return_value="creation-token"), patch("updater.subprocess.Popen", side_effect=start):
            self.updater.launch_install(self.prepared, 1111)
        return process

    def test_ready_handoff_requires_matching_live_child(self):
        process = self.launch_with_report({"ready": True, "pid": 4321})
        process.poll.assert_called_once()

    def test_wrong_or_missing_child_pid_cannot_close_parent(self):
        for report in ({"ready": True, "pid": 9999}, {"ready": True},
                       {"ready": True, "pid": "4321"}, {"ready": True, "pid": True}):
            with self.subTest(report=report):
                with self.assertRaises(UpdateError):
                    self.launch_with_report(report)

    def test_worker_that_exits_after_writing_ready_is_not_accepted(self):
        with self.assertRaises(UpdateError) as caught:
            self.launch_with_report({"ready": True, "pid": 4321}, exit_code=1)
        self.assertIn("提前退出", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
