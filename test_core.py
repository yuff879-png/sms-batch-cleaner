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
        self.info = {"model": "test", "android": "10"}
        self.target = {"event": "target", "id": "15", "fingerprint": "f" * 64}

    def prepare(self):
        def bridge(serial, action, on_record=None, **kwargs):
            on_record(self.target)
            return {"event": "summary", "count": 1, "types": {"1": 1}}
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
            return {"event": "summary", "count": 2, "types": {"1": 2}}
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


if __name__ == "__main__":
    unittest.main()
