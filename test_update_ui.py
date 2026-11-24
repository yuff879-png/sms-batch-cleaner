"""Update and phone-operation exclusion checks. Network and phone APIs are mocked."""

from pathlib import Path
import json
import tempfile
from types import SimpleNamespace
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import app


class SafeUpdateError(Exception):
    pass


class UpdateCancelled(SafeUpdateError):
    pass


class ImmediateThread:
    """Run the worker without advancing Tk; callbacks still pass through its queue."""

    def __init__(self, target, **_kwargs):
        self.target = target

    def start(self):
        self.target()


class UpdateUiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sms-cleaner-update-ui-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        resources = self.folder / "app"
        resources.mkdir()
        (self.folder / "portable.flag").touch()
        self.root = tk.Tk()
        self.root.withdraw()
        self.ui = app.CleanerApp(self.root, self.folder / "data", resources, smoke_test=True)
        # No timers or real API initialization are allowed in these tests.
        for timer in self.root.tk.call("after", "info"):
            self.root.after_cancel(timer)
        self.ui.cleaner = Mock()
        self.ui.updater = Mock()
        self.ui.update_error_type = SafeUpdateError
        self.ui.update_cancel_type = UpdateCancelled
        self.ui.devices = [{"serial": "MOCK_DEVICE_1234", "state": "device", "model": "Mock phone"}]
        self.ui.device_choice.configure(values=["Mock phone"])
        self.ui.device_choice.current(0)
        self.snapshot = SimpleNamespace(serial="MOCK_DEVICE_1234", count=2, types={"1": 2}, model="Mock phone", android="15")
        self.ui._plan_ready(self.snapshot)
        self.release = SimpleNamespace(version="9.9.9", notes="更新说明")
        self.ui.latest_release = self.release
        self.ui._update_controls()
        thread_patch = patch.object(app.threading, "Thread", ImmediateThread)
        thread_patch.start()
        self.addCleanup(thread_patch.stop)
        self.addCleanup(self.close_window)

    def close_window(self):
        if not self.ui.closing:
            self.ui.busy = False
            self.ui._close()

    def drain(self):
        self.ui._drain_events()

    def test_cancel_delete_confirmation_preserves_snapshot_without_deleting(self):
        with patch.object(app.messagebox, "askokcancel", return_value=False) as confirmation, patch("tkinter.simpledialog.askstring", side_effect=AssertionError("删除确认不应要求输入文字")):
            self.ui.delete_sms()
        self.ui.cleaner.delete.assert_not_called()
        self.ui.cleaner.discard_plan.assert_not_called()
        self.assertIs(self.ui.plan_result, self.snapshot)
        self.assertFalse(self.ui.busy)
        self.assertEqual(str(self.ui.delete_button["state"]), "normal")
        confirmation.assert_called_once()
        prompt = confirmation.call_args.args[1]
        self.assertIn(self.snapshot.model, prompt)
        self.assertIn(self.snapshot.serial[-4:], prompt)
        self.assertIn(f"{self.snapshot.count:,} 条短信", prompt)
        self.assertNotIn(self.snapshot.serial, prompt)
        self.assertNotIn("输入", prompt)
        self.assertIs(confirmation.call_args.kwargs["parent"], self.root)
        self.assertEqual(confirmation.call_args.kwargs["icon"], "warning")
        self.assertEqual(confirmation.call_args.kwargs["default"], "cancel")

    def test_accept_delete_confirmation_deletes_selected_snapshot_once(self):
        self.ui.cleaner.delete.return_value = {
            "deleted": 2, "missing": 0, "changed": 0, "failed": 0,
            "remaining_target": 0, "remaining_total": 0, "verified": True,
            "cancelled": False,
        }
        with patch.object(app.messagebox, "askokcancel", return_value=True) as confirmation, patch("tkinter.simpledialog.askstring", side_effect=AssertionError("删除确认不应要求输入文字")), patch.object(app.messagebox, "showinfo"):
            self.ui.delete_sms()
            self.assertTrue(self.ui.busy)
            self.assertIsNone(self.ui.plan_result)
            self.ui.delete_sms()
            self.drain()
            self.ui.delete_sms()
        confirmation.assert_called_once()
        self.ui.cleaner.delete.assert_called_once_with(self.snapshot)
        self.ui.cleaner.discard_plan.assert_called_once_with(self.snapshot)
        self.assertFalse(self.ui.busy)
        self.assertIsNone(self.ui.plan_result)
        self.assertEqual(str(self.ui.delete_button["state"]), "disabled")

    def test_phone_operation_blocks_check_download_and_update_controls(self):
        self.ui.operation = "清空短信"
        self.ui.operation_domain = "phone"
        self.ui._set_busy(True)
        self.ui.check_updates()
        self.ui.download_update()
        self.ui.updater.check.assert_not_called()
        self.ui.updater.download.assert_not_called()
        self.assertEqual(str(self.ui.check_update_button["state"]), "disabled")
        self.assertEqual(str(self.ui.install_update_button["state"]), "disabled")

    def test_update_blocks_all_phone_entrypoints_and_closing(self):
        self.ui.operation = "下载更新"
        self.ui.operation_domain = "update"
        self.ui._set_busy(True)
        self.ui.refresh_devices()
        self.ui.probe_device()
        self.ui.delete_sms()
        self.ui.cleaner.list_devices.assert_not_called()
        self.ui.cleaner.prepare.assert_not_called()
        self.ui.cleaner.delete.assert_not_called()
        self.assertIs(self.ui.plan_result, self.snapshot)
        self.assertEqual(str(self.ui.device_choice["state"]), "disabled")
        self.assertEqual(str(self.ui.cancel_update_button["state"]), "normal")
        with patch.object(app.messagebox, "showinfo") as message:
            self.ui._close()
        message.assert_called_once()
        self.assertFalse(self.ui.closing)

    def test_no_update_is_displayed_without_popup_or_install(self):
        self.ui.updater.check.return_value = None
        with patch.object(app.messagebox, "showinfo") as popup:
            self.ui.check_updates(automatic=True)
            self.drain()
        popup.assert_not_called()
        self.assertIn("已是最新版本", self.ui.update_status.get())
        self.assertIsNone(self.ui.latest_release)
        self.assertFalse(self.ui.busy)
        self.ui.updater.download.assert_not_called()
        self.ui.updater.launch_install.assert_not_called()

    def test_automatic_failure_is_logged_without_popup(self):
        self.ui.updater.check.side_effect = SafeUpdateError("更新服务器暂不可用")
        with patch.object(app.messagebox, "showerror") as popup:
            self.ui.check_updates(automatic=True)
            self.drain()
        popup.assert_not_called()
        self.assertIn("更新服务器暂不可用", self.ui.update_status.get())
        self.assertIn("更新服务器暂不可用", "\n".join(self.ui.log_lines))
        self.assertFalse(self.ui.busy)

    def test_manual_failure_can_be_copied_and_does_not_invalidate_phone_plan(self):
        self.ui.updater.check.side_effect = SafeUpdateError("网络不可用")
        with patch.object(app.messagebox, "showerror") as popup:
            self.ui.check_updates()
            self.drain()
        popup.assert_called_once()
        self.assertIs(self.ui.plan_result, self.snapshot)
        self.assertEqual(str(self.ui.delete_button["state"]), "normal")

    def test_update_check_works_when_phone_core_is_unavailable(self):
        self.ui.cleaner = None
        self.ui.updater.check.return_value = self.release
        self.ui.check_updates()
        self.drain()
        self.ui.updater.check.assert_called_once_with(app.VERSION)
        self.assertIn("9.9.9", self.ui.update_status.get())
        self.assertEqual(str(self.ui.install_update_button["state"]), "normal")

    def test_source_mode_allows_check_but_disables_install(self):
        self.ui.portable_install = False
        self.ui.updater.check.return_value = self.release
        self.ui.check_updates()
        self.drain()
        self.assertEqual(str(self.ui.install_update_button["state"]), "disabled")
        self.ui.download_update()
        self.ui.updater.download.assert_not_called()
        self.assertIn("开发环境", self.ui.update_status.get())

    def test_rejecting_update_prompt_never_downloads_or_discards_plan(self):
        with patch.object(app.messagebox, "askyesno", return_value=False):
            self.ui.download_update()
        self.ui.updater.download.assert_not_called()
        self.ui.cleaner.discard_plan.assert_not_called()
        self.assertIs(self.ui.plan_result, self.snapshot)

    def test_confirmed_update_discards_phone_plan_and_closes_only_after_installer_ready(self):
        prepared = object()
        self.ui.updater.download.return_value = prepared
        with patch.object(app.messagebox, "askyesno", return_value=True), patch.object(self.ui, "_close") as close:
            self.ui.download_update()
            self.assertTrue(self.ui.busy)
            self.assertIsNone(self.ui.plan_result)
            close.assert_not_called()
            self.drain()
            self.ui.updater.launch_install.assert_called_once_with(prepared, app.os.getpid())
            close.assert_called_once()
        self.ui.cleaner.discard_plan.assert_called_once_with(self.snapshot)
        self.ui.cleaner.delete.assert_not_called()

    def test_download_cancellation_does_not_launch_installer(self):
        self.ui.updater.download.side_effect = UpdateCancelled()
        with patch.object(app.messagebox, "askyesno", return_value=True), patch.object(app.messagebox, "showerror") as popup:
            self.ui.download_update()
            self.drain()
        popup.assert_not_called()
        self.ui.updater.launch_install.assert_not_called()
        self.assertIn("已取消", self.ui.update_status.get())
        self.assertFalse(self.ui.busy)

    def test_late_cancel_between_download_and_ui_callback_does_not_install(self):
        self.ui.updater.download.return_value = object()
        with patch.object(app.messagebox, "askyesno", return_value=True):
            self.ui.download_update()
        self.ui.cancel_update()
        self.ui.updater.cancel.assert_called_once()
        self.drain()
        self.ui.updater.launch_install.assert_not_called()
        self.assertIn("已取消", self.ui.update_status.get())

    def test_installer_launch_failure_keeps_window_open(self):
        self.ui.updater.download.return_value = object()
        self.ui.updater.launch_install.side_effect = SafeUpdateError("更新窗口未就绪")
        with patch.object(app.messagebox, "askyesno", return_value=True), patch.object(app.messagebox, "showerror"), patch.object(self.ui, "_close") as close:
            self.ui.download_update()
            self.drain()
            close.assert_not_called()
        self.assertFalse(self.ui.busy)
        self.assertIn("更新窗口未就绪", self.ui.update_status.get())

    def test_download_progress_accepts_done_total_and_speed(self):
        self.ui._show_update_progress({"phase": "download", "done": 50, "total": 100, "speed": 2097152})
        self.assertIn("50%", self.ui.update_status.get())
        self.assertIn("2.0 MB/s", self.ui.update_status.get())
        self.assertEqual(float(self.ui.update_progress_bar["value"]), 50)

    def test_auto_check_runs_at_most_once_and_busy_wait_is_bounded(self):
        self.ui.updater.check.return_value = None
        self.ui._automatic_update_check()
        self.drain()
        self.ui._automatic_update_check()
        self.ui.updater.check.assert_called_once()
        self.ui.automatic_check_done = False
        self.ui.busy = True
        self.ui.automatic_check_waits = 30
        with patch.object(self.root, "after") as schedule:
            self.ui._automatic_update_check()
        schedule.assert_not_called()
        self.assertTrue(self.ui.automatic_check_done)


class StartupReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sms-cleaner-startup-report-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)

    def test_report_is_atomic_and_contains_only_readiness_version_and_pid(self):
        target = self.folder / "nested" / "startup.json"
        application = SimpleNamespace(cleaner=object(), log=Mock())
        self.assertTrue(app.write_startup_report(target, application))
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")),
                         {"ready": True, "version": app.VERSION, "pid": app.os.getpid()})
        self.assertFalse(target.with_name(target.name + ".tmp").exists())
        application.cleaner = None
        self.assertTrue(app.write_startup_report(target, application))
        self.assertFalse(json.loads(target.read_text(encoding="utf-8"))["ready"])

    def test_write_failure_logs_safe_error_and_preserves_previous_report(self):
        target = self.folder / "startup.json"
        target.write_text("previous-report", encoding="utf-8")
        application = SimpleNamespace(cleaner=object(), log=Mock())
        with patch.object(Path, "replace", side_effect=OSError("PRIVATE_PATH_AND_DATA")):
            self.assertFalse(app.write_startup_report(target, application))
        self.assertEqual(target.read_text(encoding="utf-8"), "previous-report")
        self.assertFalse(target.with_name(target.name + ".tmp").exists())
        application.log.assert_called_once()
        self.assertNotIn("PRIVATE_", application.log.call_args.args[0])

    def test_main_schedules_report_only_after_entering_event_loop(self):
        target = self.folder / "startup.json"
        root = Mock()
        application = SimpleNamespace(cleaner=object(), log=Mock())
        callbacks = []
        root.after.side_effect = lambda delay, callback: callbacks.append((delay, callback))

        def enter_loop():
            self.assertFalse(target.exists())
            self.assertEqual(callbacks[0][0], 250)
            callbacks[0][1]()

        root.mainloop.side_effect = enter_loop
        with patch.object(app.sys, "argv", ["app.py", "--startup-report", str(target)]), patch.object(app.tk, "Tk", return_value=root), patch.object(app, "CleanerApp", return_value=application), patch.object(app, "application_paths", return_value=(self.folder, self.folder)):
            self.assertEqual(app.main(), 0)
        self.assertTrue(target.is_file())

    def test_smoke_mode_never_writes_startup_readiness(self):
        target = self.folder / "startup.json"
        root = Mock()
        with patch.object(app.sys, "argv", ["app.py", "--smoke-test", "--startup-report", str(target)]), patch.object(app.tk, "Tk", return_value=root), patch.object(app, "CleanerApp"), patch.object(app, "application_paths", return_value=(self.folder, self.folder)):
            self.assertEqual(app.main(), 0)
        root.after.assert_not_called()
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
