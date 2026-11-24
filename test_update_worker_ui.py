"""Keep installer controls visible without starting an actual installation."""
from pathlib import Path
import tempfile
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

import update_worker


class InstallerLayoutTests(unittest.TestCase):
    def test_buttons_stay_visible_at_default_and_minimum_window_sizes(self):
        with tempfile.TemporaryDirectory() as directory:
            checked = []

            def inspect_window(root):
                # Do not enter the event loop: the installer work callback must not run.
                for timer in root.tk.call("after", "info"):
                    root.after_cancel(timer)
                text = next(item for item in root.winfo_children() if isinstance(item, tk.Text))
                frame = next(item for item in root.winfo_children() if isinstance(item, ttk.Frame))
                buttons = frame.winfo_children()
                for size in ("720x480", "560x360"):
                    root.geometry(size)
                    root.update()
                    root.update_idletasks()
                    bottom = root.winfo_rooty() + root.winfo_height()
                    for button in buttons:
                        self.assertGreaterEqual(button.winfo_height(), button.winfo_reqheight())
                        self.assertLessEqual(button.winfo_rooty() + button.winfo_height(), bottom)
                        self.assertGreaterEqual(button.winfo_rooty(), text.winfo_rooty() + text.winfo_height())
                    checked.append(size)
                text.insert("end", "安装窗口测试日志")
                buttons[0].invoke()
                self.assertEqual(root.clipboard_get(), "安装窗口测试日志")
                root.destroy()

            with patch("sys.argv", ["update_worker.py", "--install-dir", directory, "--recover"]), \
                    patch.object(tk.Tk, "mainloop", inspect_window), \
                    patch.object(update_worker, "recover_install") as recovery:
                self.assertEqual(update_worker.main(), 0)
                recovery.assert_not_called()
            self.assertEqual(len(checked), 2)


if __name__ == "__main__":
    unittest.main()
