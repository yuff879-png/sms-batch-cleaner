import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core import Snapshot, Cleaner, CleanerError, file_hash, fingerprint


class CleanupSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "adb.exe").touch()
        self.logs = []
        self.cleaner = Cleaner(self.root, self.root, self.logs.append)
        self.serial = "test-device-one"
        self.cleaner._probed.add(self.serial)
        self.info = {"model": "test", "android": "10", "uid": 1001,
                     "read_sms_mode": 0, "write_sms_mode": 0, "count": 1, "types": {"1": 1}}
        self.target = {"event": "target", "id": "15", "fingerprint": "f" * 64}

    def prepare(self):
        def bridge(serial, action, on_record=None, **kwargs):
            on_record(self.target)
            return {**self.info, "event": "summary"}
        with patch.object(self.cleaner, "probe", return_value=self.info), patch.object(self.cleaner, "_run_bridge", side_effect=bridge):
            return self.cleaner.prepare(self.serial)

    def test_unicode_null_fingerprint_protocol(self):
        row = dict(zip(("_id", "date", "address", "body", "type", "thread_id"),
                       ["1", "2", None, "缅\n甸", "1", "3"]))
        expected = hashlib.sha256(b"1:11:2-1:7:" + "缅\n甸".encode() + b"1:11:3").hexdigest()
        self.assertEqual(fingerprint(row), expected)

    def test_plan_contains_only_exact_target_without_body(self):
        snapshot = self.prepare()
        plan = self.root / "plan"
        self.cleaner._make_plan(snapshot, plan)
        self.assertEqual(json.loads(plan.read_text()), {"id": "15", "fingerprint": "f" * 64})
        self.assertNotIn("body", snapshot.path.read_text())
        self.assertEqual(file_hash(snapshot.path), snapshot.sha256)

    def test_altered_plan_cannot_delete(self):
        snapshot = self.prepare()
        snapshot.path.write_text("corrupted", encoding="utf-8")
        with patch.object(self.cleaner, "_run_bridge") as bridge:
            with self.assertRaises(CleanerError):
                self.cleaner.delete(snapshot)
            bridge.assert_not_called()

    def test_plan_cannot_be_reassigned_to_another_device(self):
        snapshot = self.prepare()
        wrong = Snapshot(snapshot.path, snapshot.count, snapshot.types, "other-device", snapshot.sha256, "test", "10")
        self.cleaner._probed.add(wrong.serial)
        with patch.object(self.cleaner, "_run_bridge") as bridge:
            with self.assertRaises(CleanerError):
                self.cleaner.delete(wrong)
            bridge.assert_not_called()

    def test_duplicate_ids_rejected_without_usable_plan(self):
        def bridge(serial, action, on_record=None):
            on_record(self.target)
            on_record(self.target)
        with patch.object(self.cleaner, "probe", return_value=self.info), patch.object(self.cleaner, "_run_bridge", side_effect=bridge):
            with self.assertRaises(CleanerError):
                self.cleaner.prepare(self.serial)
        self.assertFalse(list((self.root / "plans").glob("*.jsonl")))

    def test_incomplete_scan_rejected(self):
        def bridge(serial, action, on_record=None):
            on_record(self.target)
            return {**self.info, "event": "summary", "count": 2, "types": {"1": 2}}
        with patch.object(self.cleaner, "probe", return_value=self.info), patch.object(self.cleaner, "_run_bridge", side_effect=bridge):
            with self.assertRaises(CleanerError):
                self.cleaner.prepare(self.serial)

    def test_bad_fingerprint_format_rejected(self):
        def bridge(serial, action, on_record=None):
            on_record(dict(self.target, fingerprint="not-a-fingerprint"))
        with patch.object(self.cleaner, "probe", return_value=self.info), patch.object(self.cleaner, "_run_bridge", side_effect=bridge):
            with self.assertRaises(CleanerError):
                self.cleaner.prepare(self.serial)

    def test_empty_and_unprobed_targets_never_reach_delete(self):
        with patch.object(self.cleaner, "_run_bridge") as bridge:
            for serial, count in [(self.serial, 0), ("unknown", 1)]:
                with self.assertRaises(CleanerError):
                    self.cleaner.delete(Snapshot(self.root / "x", count, {}, serial, "", "test", "10"))
            bridge.assert_not_called()

    def test_missing_verification_not_reported_as_success(self):
        snapshot = self.prepare()
        with patch.object(self.cleaner, "_run_bridge", return_value={"deleted": 1}):
            with self.assertRaises(CleanerError):
                self.cleaner.delete(snapshot)
        self.assertFalse(snapshot.path.exists())

    def test_partial_delete_and_new_messages_are_preserved_in_result(self):
        snapshot = self.prepare()
        expected = {"deleted": 0, "changed": 1, "remaining_target": 1,
                    "remaining_total": 4, "cancelled": True}
        with patch.object(self.cleaner, "_run_bridge", return_value=expected):
            self.assertEqual(self.cleaner.delete(snapshot), expected)
        self.assertFalse(snapshot.path.exists())

    def test_discard_cannot_delete_arbitrary_file(self):
        arbitrary = self.root / "user-file.txt"
        arbitrary.write_text("keep")
        self.cleaner.discard_plan(Snapshot(arbitrary, 1, {}, self.serial, "", "test", "10"))
        self.assertTrue(arbitrary.exists())

    def test_allowed_read_and_write_can_report_genuine_zero(self):
        summary = {**self.info, "count": 0, "types": {}}
        with patch.object(self.cleaner, "_run_bridge", return_value=summary):
            self.assertEqual(self.cleaner.probe(self.serial)["count"], 0)
        self.assertIn(self.serial, self.cleaner._probed)
        self.assertIn("本机 SMS 共 0 条", "\n".join(self.logs))

    def test_restricted_read_never_reports_zero_or_keeps_previous_probe(self):
        for mode in (1, 2, 3, 4, 5, -1):
            with self.subTest(mode=mode):
                self.logs.clear()
                self.cleaner._probed.add(self.serial)
                summary = {**self.info, "count": 0, "read_sms_mode": mode}
                with patch.object(self.cleaner, "_run_bridge", return_value=summary):
                    with self.assertRaises(CleanerError) as caught:
                        self.cleaner.probe(self.serial)
                self.assertIn("读取受限", str(caught.exception))
                self.assertIn("数量未知", str(caught.exception))
                self.assertNotIn("0 条", str(caught.exception) + "\n".join(self.logs))
                self.assertNotIn(self.serial, self.cleaner._probed)

    def test_missing_or_invalid_read_mode_fails_closed_without_echo(self):
        values = (None, False, True, "0", 0.0, "PRIVATE_PHONE_BODY", {}, [])
        for mode in values:
            with self.subTest(mode=mode):
                self.logs.clear()
                summary = {**self.info, "count": 0, "read_sms_mode": mode}
                with patch.object(self.cleaner, "_run_bridge", return_value=summary):
                    with self.assertRaises(CleanerError) as caught:
                        self.cleaner.probe(self.serial)
                combined = str(caught.exception) + "\n".join(self.logs)
                self.assertIn("read_sms_mode=unknown", combined)
                self.assertNotIn("PRIVATE_PHONE_BODY", combined)
                self.assertNotIn(self.serial, self.cleaner._probed)
        summary = dict(self.info)
        del summary["read_sms_mode"]
        with patch.object(self.cleaner, "_run_bridge", return_value=summary):
            with self.assertRaises(CleanerError):
                self.cleaner.probe(self.serial)

    def test_write_mode_requires_integer_allowed(self):
        for mode in (1, 3, None, False, "0", 0.0):
            with self.subTest(mode=mode):
                with patch.object(self.cleaner, "_run_bridge", return_value={**self.info, "write_sms_mode": mode}):
                    with self.assertRaises(CleanerError):
                        self.cleaner.probe(self.serial)
                self.assertNotIn(self.serial, self.cleaner._probed)

    def test_invalid_probe_counts_are_unknown_never_success(self):
        for count in (-1, False, True, None, "0", 0.0, [], "PRIVATE_PHONE_BODY"):
            with self.subTest(count=count):
                self.logs.clear()
                with patch.object(self.cleaner, "_run_bridge", return_value={**self.info, "count": count}):
                    with self.assertRaises(CleanerError) as caught:
                        self.cleaner.probe(self.serial)
                combined = str(caught.exception) + "\n".join(self.logs)
                self.assertIn("数量未知", combined)
                self.assertNotIn("检测成功", combined)
                self.assertNotIn("PRIVATE_PHONE_BODY", combined)
                self.assertNotIn(self.serial, self.cleaner._probed)
        summary = dict(self.info)
        del summary["count"]
        with patch.object(self.cleaner, "_run_bridge", return_value=summary):
            with self.assertRaises(CleanerError):
                self.cleaner.probe(self.serial)

    def test_capability_log_has_model_and_suffix_without_raw_device_data(self):
        summary = {**self.info, "model": "Redmi 9A\n" + self.serial + " 0987654321",
                   "android": "PRIVATE_ANDROID_TEXT", "body": "PRIVATE_BODY", "address": "PRIVATE_ADDRESS"}
        with patch.object(self.cleaner, "_run_bridge", return_value=summary):
            self.cleaner.probe(self.serial)
        diagnostic = next(line for line in self.logs if line.startswith("短信能力："))
        self.assertIn("Redmi 9A", diagnostic)
        self.assertIn("serial_suffix=…_one", diagnostic)
        self.assertIn("read_sms_mode=0, write_sms_mode=0", diagnostic)
        self.assertNotIn("\n", diagnostic)
        for private in (self.serial, "0987654321", "PRIVATE_BODY", "PRIVATE_ADDRESS", "PRIVATE_ANDROID_TEXT"):
            self.assertNotIn(private, "\n".join(self.logs))

    def test_allowed_scan_can_create_zero_plan(self):
        summary = {**self.info, "count": 0, "types": {}}
        with patch.object(self.cleaner, "_run_bridge", return_value=summary):
            snapshot = self.cleaner.prepare(self.serial)
        self.assertEqual(snapshot.count, 0)
        self.assertTrue(snapshot.path.is_file())

    def test_scan_permission_change_and_invalid_zero_never_create_usable_plan(self):
        cases = ({"count": 0, "read_sms_mode": 1}, {"count": 0, "read_sms_mode": None},
                 {"count": False}, {"count": 0.0}, {"count": -1})
        for changes in cases:
            with self.subTest(changes=changes):
                self.logs.clear()
                with patch.object(self.cleaner, "probe", return_value=self.info), patch.object(
                        self.cleaner, "_run_bridge", return_value={**self.info, **changes}):
                    with self.assertRaises(CleanerError):
                        self.cleaner.prepare(self.serial)
                self.assertEqual(list((self.root / "plans").glob("*.jsonl")), [])
                self.assertNotIn(self.serial, self.cleaner._probed)
                self.assertNotIn("可清理 0 条", "\n".join(self.logs))


if __name__ == "__main__":
    unittest.main()
