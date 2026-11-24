"""Regression checks for safe, accurate ADB/root errors; no phone is contacted."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from core import Cleaner, CleanerError


class AdbErrorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sms-cleaner-adb-errors-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "adb.exe").touch()
        (self.root / "bridge.jar").touch()
        self.logs = []
        self.cleaner = Cleaner(self.root, self.root, self.logs.append)
        self.serial = "PRIVATE_DEVICE_SERIAL"

    def failure(self, diagnostic, returncode=1, stage="root_check", args=None):
        process = subprocess.CompletedProcess([], returncode, "", diagnostic)
        with patch("core.subprocess.run", return_value=process):
            with self.assertRaises(CleanerError) as caught:
                self.cleaner._command(args or ["shell", "su -c id"], serial=self.serial, stage=stage)
        return str(caught.exception)

    def test_su_missing_is_root_unavailable_not_disconnected(self):
        for diagnostic in ("su: not found", "/system/bin/sh: su: not found",
                           "/system/bin/sh: su: inaccessible or not found", ""):
            with self.subTest(diagnostic=diagnostic):
                message = self.failure(diagnostic, returncode=127)
                self.assertIn("ROOT_UNAVAILABLE", message)
                self.assertIn("未检测到可用 root", message)
                self.assertNotIn("设备已离线", message)
                self.assertNotIn("检查数据线", message)
                self.assertIn("stage=root_check", self.logs[-1])
                self.assertIn("exit=127", self.logs[-1])

    def test_su_permission_and_execution_denials_are_root_denied(self):
        for diagnostic, returncode in (("su: Permission denied", 1),
                                       ("su: Operation not permitted", 1),
                                       ("request rejected", 1),
                                       ("su: inaccessible", 126), ("", 126)):
            with self.subTest(diagnostic=diagnostic, returncode=returncode):
                self.assertIn("ROOT_DENIED", self.failure(diagnostic, returncode))

    def test_su_implementation_error_has_separate_classification(self):
        message = self.failure("su: invalid option: -c")
        self.assertIn("ROOT_COMMAND_FAILED", message)
        self.assertIn("不兼容", message)

    def test_transport_errors_win_even_during_root_check(self):
        cases = [
            (f"adb: device '{self.serial}' not found", "ADB_DEVICE_MISSING"),
            (f"error: device '{self.serial}' not found", "ADB_DEVICE_MISSING"),
            ("error: device offline", "ADB_OFFLINE"),
            ("adb: error: device offline", "ADB_OFFLINE"),
            ("error: no devices/emulators found", "ADB_DEVICE_MISSING"),
            ("adb: no devices found", "ADB_DEVICE_MISSING"),
            ("error: device unauthorized.\nPlease check the confirmation dialog.", "ADB_UNAUTHORIZED"),
        ]
        for diagnostic, expected in cases:
            with self.subTest(diagnostic=diagnostic):
                message = self.failure(diagnostic)
                self.assertIn(expected, message)
                self.assertNotIn(self.serial, message + "\n".join(self.logs))

    def test_remote_missing_command_is_not_transport_failure(self):
        message = self.failure("/system/bin/sh: am: inaccessible or not found", 127,
                               "current_user", ["shell", "am get-current-user"])
        self.assertIn("USER_COMMAND_UNAVAILABLE", message)
        self.assertNotIn("ADB_DEVICE_MISSING", message)
        self.assertNotIn("ROOT_UNAVAILABLE", message)

    def test_current_user_permission_failure_is_not_root_denial(self):
        message = self.failure("Permission denied", stage="current_user")
        self.assertIn("USER_QUERY_FAILED", message)
        self.assertNotIn("ROOT_DENIED", message)

    def test_file_permissions_and_missing_files_are_not_root_or_transport_errors(self):
        for stage in ("bridge_prepare", "bridge_upload", "plan_upload", "cancel_request", "cleanup"):
            with self.subTest(stage=stage):
                message = self.failure("chmod: Permission denied", stage=stage)
                self.assertIn("FILE_PERMISSION_DENIED", message)
                self.assertNotIn("ROOT_DENIED", message)
                message = self.failure("stat: PRIVATE_PATH: No such file or directory", stage=stage)
                self.assertIn("FILE_UNAVAILABLE", message)
                self.assertNotIn("ADB_DEVICE_MISSING", message)
                self.assertNotIn("PRIVATE_PATH", message)

    def test_incidental_diagnostic_words_do_not_impersonate_transport_errors(self):
        diagnostic = "cat: /private/device offline not found: No such file or directory"
        message = self.failure(diagnostic, stage="boot_check")
        self.assertIn("COMMAND_UNAVAILABLE", message)
        self.assertNotIn("ADB_OFFLINE", message)
        self.assertNotIn("ADB_DEVICE_MISSING", message)

    def test_logs_do_not_copy_command_output_serial_or_unrecognized_stage(self):
        secret = "PRIVATE_PHONE_AND_MESSAGE"
        diagnostic = "su: not found " + secret
        message = self.failure(diagnostic, 127, secret, ["shell", "su -c id " + secret])
        combined = message + "\n".join(self.logs)
        self.assertNotIn(secret, combined)
        self.assertNotIn(self.serial, combined)
        self.assertIn("stage=command", combined)
        self.assertIn("exit=127", combined)

    def test_success_logs_phase_and_exit_but_never_identity_output(self):
        private_output = "uid=0(root) PRIVATE_GROUPS_AND_CONTEXT"
        result = subprocess.CompletedProcess([], 0, private_output, "")
        with patch("core.subprocess.run", return_value=result):
            returned = self.cleaner._command(["shell", "su -c id"], serial=self.serial, stage="root_check")
        self.assertEqual(returned, private_output)
        self.assertIn("stage=root_check code=OK exit=0", self.logs[-1])
        self.assertNotIn(private_output, "\n".join(self.logs))
        self.assertNotIn(self.serial, "\n".join(self.logs))

    def test_timeout_and_host_launch_failure_include_safe_stage(self):
        timeout = subprocess.TimeoutExpired(["PRIVATE_COMMAND"], 45, output="PRIVATE_OUTPUT")
        with patch("core.subprocess.run", side_effect=timeout):
            with self.assertRaises(CleanerError) as caught:
                self.cleaner._command(["shell", "su -c id"], stage="root_check")
        self.assertIn("root 检测超时", str(caught.exception))
        self.assertIn("code=ADB_TIMEOUT", self.logs[-1])
        with patch("core.subprocess.run", side_effect=OSError("PRIVATE_LOCAL_PATH")):
            with self.assertRaises(CleanerError) as caught:
                self.cleaner._command(["devices", "-l"], stage="devices")
        combined = str(caught.exception) + "\n".join(self.logs)
        self.assertIn("code=ADB_LAUNCH_FAILED", combined)
        self.assertNotIn("PRIVATE_", combined)

    def test_ready_requests_root_uid_zero_without_su_user_switch_syntax(self):
        def respond(args, **kwargs):
            stage = kwargs.get("stage")
            if stage == "connection":
                return "device\n"
            if stage == "current_user":
                return "0\n"
            if stage == "root_check":
                self.assertEqual(args, ["shell", "su -c id"])
                return "uid=0(root) gid=0(root)\n"
            return ""

        with patch.object(self.cleaner, "_command", side_effect=respond) as command:
            remote = self.cleaner._ready(self.serial)
        self.assertTrue(remote.startswith("/data/local/tmp/sms-cleaner-"))
        stages = [call.kwargs.get("stage") for call in command.call_args_list]
        self.assertEqual(stages, ["connection", "current_user", "root_check", "bridge_prepare", "bridge_upload", "bridge_prepare"])

    def test_ready_nonroot_identity_blocks_bridge_upload(self):
        for identity in ("uid=2000(shell)", "uid=1001(radio)", "uid=01(invalid)"):
            with self.subTest(identity=identity):
                with patch.object(self.cleaner, "_command", side_effect=["device", "0", identity]) as command:
                    with self.assertRaises(CleanerError) as caught:
                        self.cleaner._ready(self.serial)
                self.assertIn("ROOT_IDENTITY_MISMATCH", str(caught.exception))
                self.assertEqual(command.call_count, 3)
                self.assertNotIn(identity, "\n".join(self.logs))


if __name__ == "__main__":
    unittest.main()
