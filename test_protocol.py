"""Host protocol safety checks. All phone processes and ADB commands are mocked."""

import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from core import Cleaner, CleanerError


class FakePhoneProcess:
    """A completed byte stream whose exit code is observable through wait/poll."""

    def __init__(self, output, exit_code=0):
        self.stdout = io.StringIO(output)
        self.exit_code = exit_code
        self.returncode = None
        self.terminated = False
        self.killed = False

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = self.exit_code
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


def wire(*events):
    return "".join((event if isinstance(event, str) else json.dumps(event, ensure_ascii=False)) + "\n" for event in events)


class BridgeProtocolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sms-cleaner-protocol-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "adb.exe").touch()
        self.logs = []
        self.progress = []
        self.cleaner = Cleaner(self.root, self.root, self.logs.append, self.progress.append)
        self.serial = "mock-only-phone"
        self.boot_id = "11111111-2222-3333-4444-555555555555"
        self.remote = "/data/local/tmp/sms-cleaner-mock"
        self.plan = self.root / "plan.jsonl"
        self.plan.write_text('{"id":"5","fingerprint":"' + "a" * 64 + '"}\n', encoding="utf-8")
        self.ready = self._patch(patch.object(self.cleaner, "_ready", return_value=self.remote))
        self.command = self._patch(patch.object(self.cleaner, "_command", return_value=""))
        self.boot = self._patch(patch.object(self.cleaner, "_boot_id", return_value=self.boot_id))
        self.popen = self._patch(patch("core.subprocess.Popen"))

    def _patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def phone_output(self, *events, exit_code=0):
        process = FakePhoneProcess(wire(*events), exit_code)
        self.popen.return_value = process
        return process

    def deletion_summary(self, **changes):
        result = {"event": "summary", "total": 1, "processed": 1, "deleted": 1,
                  "changed": 0, "missing": 0, "failed": 0, "remaining_target": 0,
                  "remaining_total": 0, "verified": True, "cancelled": False}
        result.update(changes)
        return result

    def run_delete(self, serial=None):
        return self.cleaner._run_bridge(serial or self.serial, "delete", plan=self.plan)

    def cleanup_commands(self):
        return [call.args[0] for call in self.command.call_args_list
                if call.args and call.args[0][0] == "shell" and call.args[0][1].startswith("rm -f ")]

    def test_valid_summary_and_zero_exit_are_required_for_success(self):
        summary = {"event": "summary", "count": 0, "types": {}, "uid": 1001,
                   "read_sms_mode": 0, "write_sms_mode": 0}
        self.phone_output(summary)
        self.assertEqual(self.cleaner._run_bridge(self.serial, "probe"), summary)
        self.phone_output(summary, exit_code=1)
        with self.assertRaises(CleanerError):
            self.cleaner._run_bridge(self.serial, "probe")
        self.phone_output(exit_code=0)
        with self.assertRaises(CleanerError):
            self.cleaner._run_bridge(self.serial, "probe")

    def test_raw_diagnostics_never_enter_log_or_progress(self):
        private = "RAW_PRIVATE_PHONE_AND_BODY"
        summary = {"event": "summary", "count": 0, "types": {}}
        self.phone_output("ART diagnostic " + private, "su: " + private, summary)
        self.assertEqual(self.cleaner._run_bridge(self.serial, "probe"), summary)
        self.assertNotIn(private, json.dumps(self.logs + self.progress))

    def test_error_contains_safe_stage_and_exception_without_private_message(self):
        secret = "PRIVATE_MESSAGE_0987654321"
        self.phone_output({"event": "error", "code": "android_permission_denied",
                           "stage": "query", "exception_class": "java.lang.SecurityException",
                           "hint": "permission_check", "classification": {"permission": True, "uid": False},
                           "message": secret, "raw_exception": secret}, exit_code=1)
        with self.assertRaises(CleanerError) as caught:
            self.cleaner._run_bridge(self.serial, "probe")
        message = str(caught.exception)
        self.assertIn("stage=query", message)
        self.assertIn("exception=java.lang.SecurityException", message)
        self.assertIn("permission", message)
        self.assertNotIn(secret, message + json.dumps(self.logs + self.progress))

    def test_read_not_allowed_is_actionable_unknown_and_logs_integer_modes_only(self):
        secret = "PRIVATE_PHONE_BODY_AND_SERIAL"
        self.cleaner._probed.add(self.serial)
        self.phone_output({"event": "error", "code": "read_not_allowed",
                           "read_sms_mode": 1, "write_sms_mode": 0, "count": 0,
                           "message": secret, "exception_class": secret, "serial": self.serial,
                           "body": secret, "stage": secret, "hint": secret}, exit_code=1)
        with self.assertRaises(CleanerError) as caught:
            self.cleaner._run_bridge(self.serial, "probe")
        combined = str(caught.exception) + "\n".join(self.logs)
        self.assertIn("读取受限", combined)
        self.assertIn("数量未知", combined)
        self.assertIn("read_sms_mode=1, write_sms_mode=0", combined)
        self.assertNotIn("0 条", combined)
        self.assertNotIn(secret, combined)
        self.assertNotIn(self.serial, combined)
        self.assertNotIn(self.serial, self.cleaner._probed)

    def test_read_error_missing_or_noninteger_modes_are_unknown_without_echo(self):
        cases = ({}, {"read_sms_mode": "PRIVATE_READ_MODE", "write_sms_mode": {"body": "PRIVATE_BODY"}},
                 {"read_sms_mode": False, "write_sms_mode": 0.0})
        for modes in cases:
            with self.subTest(modes=modes):
                self.logs.clear()
                self.phone_output({"event": "error", "code": "read_not_allowed", **modes}, exit_code=1)
                with self.assertRaises(CleanerError) as caught:
                    self.cleaner._run_bridge(self.serial, "probe")
                combined = str(caught.exception) + "\n".join(self.logs)
                self.assertIn("read_sms_mode=unknown, write_sms_mode=unknown", combined)
                self.assertNotIn("PRIVATE_", combined)
                self.assertIn("数量未知", combined)

    def test_status_events_reach_progress_for_scanning_and_verification(self):
        status = {"event": "status", "phase": "verifying", "processed": 230}
        self.phone_output(status, {"event": "summary", "count": 230, "types": {"1": 230}})
        self.cleaner._run_bridge(self.serial, "probe")
        self.assertEqual(self.progress, [status])

    def test_duplicate_summary_cannot_unlock_deletion(self):
        summary = self.deletion_summary()
        self.phone_output(summary, summary)
        with self.assertRaises(CleanerError):
            self.run_delete()
        self.assertTrue(self.cleaner._state_path(self.serial).is_file())
        self.assertFalse(self.cleanup_commands())

    def test_truncated_scan_never_becomes_usable_plan_or_persists_message_content(self):
        private = "PRIVATE_BODY_MUST_NOT_BE_SAVED"
        self.phone_output("ART diagnostic " + private,
                          {"event": "target", "id": "5", "fingerprint": "a" * 64})
        safe_info = {"uid": 1001, "read_sms_mode": 0, "write_sms_mode": 0, "model": "Mock phone",
                     "android": "15", "count": 1, "types": {"1": 1}}
        with patch.object(self.cleaner, "probe", return_value=safe_info):
            with self.assertRaises(CleanerError):
                self.cleaner.prepare(self.serial)
        folder = self.root / "plans"
        self.assertTrue(folder.is_dir())
        self.assertEqual(list(folder.iterdir()), [])
        self.assertFalse((self.root / "backups").exists())
        self.assertNotIn(private, "\n".join(self.logs))
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(private.encode(), path.read_bytes())

    def test_unverified_or_missing_remaining_counts_keep_persistent_lock(self):
        cases = ["verified", "remaining_target", "remaining_total"]
        for index, omitted in enumerate(cases):
            with self.subTest(omitted=omitted):
                serial = self.serial + str(index)
                summary = self.deletion_summary()
                del summary[omitted]
                self.phone_output(summary)
                with self.assertRaises(CleanerError):
                    self.run_delete(serial)
                marker = self.cleaner._state_path(serial)
                self.assertTrue(marker.is_file())
                self.assertEqual(json.loads(marker.read_text(encoding="utf-8"))["boot_id"], self.boot_id)
        self.assertFalse(self.cleanup_commands())

    def test_wrong_remaining_count_type_does_not_unlock(self):
        self.phone_output(self.deletion_summary(remaining_target="0"))
        with self.assertRaises(CleanerError):
            self.run_delete()
        self.assertTrue(self.cleaner._state_path(self.serial).is_file())

    def test_nonzero_exit_keeps_lock_even_after_verified_summary(self):
        self.phone_output(self.deletion_summary(), exit_code=1)
        with self.assertRaises(CleanerError):
            self.run_delete()
        self.assertTrue(self.cleaner._state_path(self.serial).is_file())
        self.assertFalse(self.cleanup_commands())

    def test_unconfirmed_process_exit_preserves_lock_and_requests_stop(self):
        process = self.phone_output(self.deletion_summary())
        process.wait = Mock(side_effect=[subprocess.TimeoutExpired("mock-phone", 10), -15])
        with self.assertRaises(CleanerError):
            self.run_delete()
        self.assertTrue(self.cleaner._state_path(self.serial).is_file())
        self.assertTrue(process.terminated)
        self.assertTrue(any(call.args[0][0] == "shell" and call.args[0][1].startswith("touch ")
                            for call in self.command.call_args_list))
        self.assertFalse(self.cleanup_commands())

    def test_connection_loss_without_summary_keeps_lock_and_stop_marker(self):
        self.phone_output({"event": "progress", "processed": 1, "deleted": 1}, exit_code=255)
        with self.assertRaises(CleanerError):
            self.run_delete()
        self.assertTrue(self.cleaner._state_path(self.serial).is_file())
        self.assertFalse(self.cleanup_commands())

    def test_confirmed_complete_result_removes_lock_and_remote_plan(self):
        summary = self.deletion_summary()
        self.phone_output(summary)
        self.assertEqual(self.run_delete(), summary)
        self.assertFalse(self.cleaner._state_path(self.serial).exists())
        cleanup = self.cleanup_commands()
        self.assertEqual(len(cleanup), 1)
        self.assertIn("/plan-", cleanup[0][1])
        self.assertIn("/cancel-", cleanup[0][1])

    def test_confirmed_cancel_is_returned_as_partial_not_success_or_unknown(self):
        summary = self.deletion_summary(deleted=0, processed=0, remaining_target=1,
                                        remaining_total=3, cancelled=True)
        self.cleaner.cancel()
        self.phone_output(summary)
        result = self.run_delete()
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["remaining_target"], 1)
        self.assertFalse(self.cleaner._state_path(self.serial).exists())
        self.assertTrue(any(call.args[0][0] == "shell" and call.args[0][1].startswith("touch ")
                            for call in self.command.call_args_list))

    def test_launch_failure_removes_lock_because_phone_process_never_started(self):
        self.popen.side_effect = OSError("private host path must not appear in error")
        with self.assertRaises(CleanerError) as caught:
            self.run_delete()
        self.assertFalse(self.cleaner._state_path(self.serial).exists())
        self.assertNotIn("private host path", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
