"""Separate update window and recoverable portable-directory replacement."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid

from updater import PreparedUpdate, ROOT_FILES, UpdateError, Updater, validate_package, version_tuple

MYANMAR_TIME = timezone(timedelta(hours=6, minutes=30))


def process_identity(pid: int) -> str | None:
    """A creation-time identity prevents waiting on an accidentally reused PID."""
    if pid <= 0:
        return None
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                          ctypes.byref(kernel_time), ctypes.byref(user_time)):
                return None
            return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        return text[text.rfind(")") + 2:].split()[19]
    except (OSError, IndexError):
        return str(pid) if pid == os.getpid() else None


def wait_for_parent(pid: int, expected_start: str, timeout: float = 60,
                    progress=lambda event: None) -> None:
    started = time.monotonic()
    while process_identity(pid) == expected_start:
        elapsed = time.monotonic() - started
        progress({"phase": "waiting", "done": int(elapsed), "total": int(timeout)})
        if elapsed >= timeout:
            raise UpdateError("原程序未在 60 秒内退出，已停止更新。请关闭原程序后重试。")
        time.sleep(0.2)


class UpdateLock:
    def __init__(self, install: Path):
        self.path = install / ".update.lock"
        self.file = None

    def __enter__(self):
        self.file = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                if not self.file.read(1):
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise UpdateError("另一份更新程序正在运行，请等待它结束。") from None
        return self

    def __exit__(self, *args):
        if self.file is not None:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            self.file.close()
            self.file = None


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def _remove(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _journal_paths(install: Path, journal: dict) -> tuple[Path, list[dict]]:
    workspace_name = journal.get("workspace")
    if (journal.get("schema") != 1 or not isinstance(workspace_name, str)
            or not workspace_name.startswith("." + install.name + ".update-")
            or Path(workspace_name).name != workspace_name):
        raise UpdateError("更新恢复记录无效，请保留现场并联系维护人员。")
    workspace = install.parent / workspace_name
    items = journal.get("items")
    if not isinstance(items, list):
        raise UpdateError("更新恢复清单无效。")
    seen = set()
    for item in items:
        if (not isinstance(item, dict) or item.get("name") not in ({"app", "runtime"} | ROOT_FILES)
                or item.get("name") in seen or type(item.get("old_exists")) is not bool):
            raise UpdateError("更新恢复清单包含无效路径。")
        seen.add(item["name"])
    return workspace, items


def recover_install(install_dir: Path, log=lambda text: None) -> bool:
    """Call while holding UpdateLock. Roll back an interrupted replacement."""
    install = Path(install_dir).resolve()
    journal_path = install / ".update-journal.json"
    if not journal_path.exists():
        return False
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise UpdateError("更新恢复记录无法读取；已保留旧版本备份。") from None
    workspace, items = _journal_paths(install, journal)
    if journal.get("phase") == "complete":
        # A validated update committed successfully; a later cleanup interruption
        # is not a reason to undo it.
        try:
            _remove(workspace)
        except OSError as error:
            raise UpdateError("当前版本已安装成功，但上次更新的旧文件仍被其他程序占用或无法删除。"
                              "请关闭其他使用本工具目录的程序后重试更新；不会自动结束 ADB 服务。") from error
        journal_path.unlink()
        return False
    log("正在恢复更新前的程序文件……")
    for item in reversed(items):
        destination = install / item["name"]
        backup = workspace / "old" / item["name"]
        ready = workspace / "new" / item["name"]
        if backup.exists():
            _remove(destination)
            os.replace(backup, destination)
        elif not item["old_exists"] and item.get("started") and not ready.exists():
            _remove(destination)
    _remove(workspace)
    journal_path.unlink()
    log("已恢复更新前的版本，用户数据保持原样。")
    return True


def _copy_tree(source: Path, destination: Path, copy_file, progress, counter: list[int], total: int) -> None:
    destination.mkdir(parents=True)
    for entry in source.iterdir():
        if entry.is_symlink():
            raise UpdateError("更新暂存目录包含符号链接，已停止安装。")
        target = destination / entry.name
        if entry.is_dir():
            _copy_tree(entry, target, copy_file, progress, counter, total)
        elif entry.is_file():
            copy_file(entry, target)
            counter[0] += 1
            progress({"phase": "copying", "done": counter[0], "total": total})
        else:
            raise UpdateError("更新暂存目录包含不支持的文件类型。")


def smoke_test(install: Path) -> None:
    command = [str(install / "runtime/pythonw.exe"), str(install / "app/app.py"), "--smoke-test"]
    try:
        result = subprocess.run(command, cwd=install, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=45, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        raise UpdateError("新版本启动检查失败或超时。") from None
    if result.returncode != 0:
        raise UpdateError(f"新版本启动检查失败（退出码 {result.returncode}）。")


def install_package(install_dir: Path, package_dir: Path, version: str,
                    progress=lambda event: None, log=lambda text: None, *,
                    copy_file=shutil.copy2, rename=os.replace, verify=smoke_test,
                    already_locked: bool = False) -> None:
    """Copy first, rename with a recovery journal, verify, and roll back on error.

    copy_file/rename/verify are injectable for bounded failure tests. No path in
    install/data is replaced, copied, enumerated, or restored here.
    """
    install, package = Path(install_dir).resolve(), Path(package_dir).resolve()
    if not already_locked:
        with UpdateLock(install):
            return install_package(install, package, version, progress, log, copy_file=copy_file,
                                   rename=rename, verify=verify, already_locked=True)
    version_tuple(version)
    validate_package(package, version)
    if package == install or package.is_relative_to(install / "app") or package.is_relative_to(install / "runtime"):
        raise UpdateError("更新程序必须从独立暂存目录运行。")
    recover_install(install, log)
    workspace = install.parent / ("." + install.name + ".update-" + uuid.uuid4().hex)
    new, old = workspace / "new", workspace / "old"
    new.mkdir(parents=True)
    old.mkdir()
    names = ["app", "runtime"] + sorted(name for name in ROOT_FILES if (package / name).is_file())
    items = [{"name": name, "old_exists": (install / name).exists(), "started": False} for name in names]
    journal = {"schema": 1, "workspace": workspace.name, "version": version, "phase": "preparing", "items": items}
    journal_path = install / ".update-journal.json"
    verified_process = None
    try:
        total = sum(1 for name in names for p in ([package / name] if (package / name).is_file()
                    else (package / name).rglob("*")) if p.is_file())
        counter = [0]
        log("正在复制新版程序和运行环境……")
        for name in names:
            source = package / name
            if source.is_dir():
                _copy_tree(source, new / name, copy_file, progress, counter, total)
            else:
                copy_file(source, new / name)
                counter[0] += 1
                progress({"phase": "copying", "done": counter[0], "total": total})
        validate_package(new, version)
        _write_json(journal_path, journal)
        log("正在替换程序文件，保留旧版本用于回滚……")
        journal["phase"] = "replacing"
        for number, item in enumerate(items, 1):
            item["started"] = True
            _write_json(journal_path, journal)
            name = item["name"]
            if item["old_exists"]:
                rename(install / name, old / name)
            rename(new / name, install / name)
            progress({"phase": "replacing", "done": number, "total": len(items)})
        journal["phase"] = "verifying"
        _write_json(journal_path, journal)
        log("正在验证新版本能否正常启动……")
        progress({"phase": "verifying", "done": 0, "total": 1})
        verified_process = verify(install)
        progress({"phase": "verifying", "done": 1, "total": 1})
        journal["phase"] = "complete"
        _write_json(journal_path, journal)
    except BaseException as failure:
        # Production verification may have opened the new main window. If a
        # later journal/commit step fails, stop only that owned process before
        # restoring the old files; never leave a new window running old paths.
        if verified_process is not None:
            _stop_owned_process(verified_process)
        if journal_path.exists():
            try:
                recover_install(install, log)
            except BaseException as rollback:
                raise UpdateError("更新失败且自动恢复未完成。请保留程序目录和同级 .update 备份，"
                                  "使用恢复入口重试。恢复错误：" + str(rollback)[:300]) from failure
        else:
            _remove(workspace)
        if isinstance(failure, OSError) and (getattr(failure, "winerror", None) in (5, 32, 33)
                                             or getattr(failure, "errno", None) == 13):
            raise UpdateError("程序文件被占用或没有写入权限，旧版本已恢复。"
                              "请关闭该工具的其他窗口后重试。") from failure
        raise
    try:
        _remove(workspace)
        journal_path.unlink()
    except OSError:
        log("新版本已验证通过；部分旧文件暂时无法清理，将在下次更新时处理。")
    log(f"版本 {version} 安装并验证成功。")


def _stop_owned_process(process) -> None:
    """Stop only a Popen child created by this worker, never another instance."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def restart_app(install: Path, expected_version: str | None,
                progress=lambda event: None):
    report = install / "data" / ("update-start-" + uuid.uuid4().hex + ".json")
    report.parent.mkdir(parents=True, exist_ok=True)
    process = None
    try:
        process = subprocess.Popen([str(install / "runtime/pythonw.exe"), str(install / "app/app.py"),
                                    "--startup-report", str(report)],
                                   cwd=install, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        started = time.monotonic()
        deadline = started + 15
        while time.monotonic() < deadline:
            progress({"phase": "restarting", "done": int(time.monotonic() - started), "total": 15})
            if process.poll() is not None:
                raise UpdateError("新主窗口启动后提前退出，更新不会提交。")
            if report.is_file():
                try:
                    status = json.loads(report.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    status = {}
                if (status.get("ready") is True and status.get("pid") == process.pid
                        and (expected_version is None or status.get("version") == expected_version)
                        and process.poll() is None):
                    report.unlink(missing_ok=True)
                    return process
            time.sleep(0.1)
        raise UpdateError("新主窗口未在 15 秒内确认启动成功，更新不会提交。")
    except BaseException as failure:
        if process is not None:
            _stop_owned_process(process)
        report.unlink(missing_ok=True)
        if isinstance(failure, OSError):
            raise UpdateError("无法打开新主窗口，更新不会提交。") from failure
        raise


def verify_and_restart(install: Path, version: str, progress=lambda event: None,
                       log=lambda message: None):
    smoke_test(install)
    log("启动检查通过，正在打开新主窗口并等待确认……")
    return restart_app(install, version, progress)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-dir", type=Path, required=True)
    parser.add_argument("--package-dir", type=Path)
    parser.add_argument("--version")
    parser.add_argument("--parent-pid", type=int, default=0)
    parser.add_argument("--parent-start", default="")
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--channel-dir", type=Path)
    parser.add_argument("--recover", action="store_true")
    args = parser.parse_args()
    install = args.install_dir.resolve()
    if not install.is_dir():
        return 2
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    import tkinter as tk
    from tkinter import ttk
    root = tk.Tk()
    root.title("短信清理工具 · 安装更新")
    root.geometry("720x480")
    root.minsize(560, 360)
    root.configure(padx=20, pady=18)
    root.columnconfigure(0, weight=1)
    root.rowconfigure(2, weight=1)
    title = tk.StringVar(value="正在准备更新窗口……")
    ttk.Label(root, textvariable=title, font=("Microsoft YaHei UI", 12, "bold"),
              wraplength=650).grid(row=0, column=0, sticky="w")
    bar = ttk.Progressbar(root, maximum=100)
    bar.grid(row=1, column=0, sticky="ew", pady=12)
    text = tk.Text(root, height=6, width=40, wrap="word", font=("Microsoft YaHei UI", 10))
    text.grid(row=2, column=0, sticky="nsew")
    buttons = ttk.Frame(root)
    buttons.grid(row=3, column=0, sticky="ew", pady=(12, 0))
    finished = threading.Event()
    events: queue.Queue = queue.Queue()
    log_dir = install / "data/logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "update.log"

    def log(message):
        message = str(message)
        with log_path.open("a", encoding="utf-8") as output:
            output.write(datetime.now(MYANMAR_TIME).strftime("%Y-%m-%d %H:%M:%S +06:30 ") + message + "\n")
        events.put(("log", message))

    def copy_error():
        root.clipboard_clear()
        root.clipboard_append(text.get("1.0", "end-1c"))

    ttk.Button(buttons, text="复制日志", command=copy_error, padding=(10, 7)).pack(side="left")
    close = ttk.Button(buttons, text="关闭", command=root.destroy, state="disabled", padding=(10, 7))
    close.pack(side="right")
    root.protocol("WM_DELETE_WINDOW", lambda: root.destroy() if finished.is_set() else None)

    def work():
        try:
            with UpdateLock(install):
                if not args.recover:
                    if args.package_dir is None or args.version is None:
                        raise UpdateError("缺少更新安装参数。")
                    channel_dir = args.channel_dir or install / "app"
                    verifier = Updater(install, channel_dir, args.package_dir.parent, log=log,
                                       progress=lambda e: events.put(("progress", e)))
                    verifier.verify_prepared(PreparedUpdate(args.package_dir.parent, args.package_dir, args.version))
                if args.ready_file:
                    _write_json(args.ready_file, {"ready": True, "pid": os.getpid()})
                if args.parent_pid:
                    log("更新窗口已接管，正在等待原程序退出……")
                    wait_for_parent(args.parent_pid, args.parent_start, progress=lambda e: events.put(("progress", e)))
                if args.recover:
                    recover_install(install, log)
                    log("正在重新打开已恢复的短信清理工具……")
                    restart_app(install, None, lambda e: events.put(("progress", e)))
                    completion = "恢复完成，主程序已重新打开。"
                else:
                    install_package(install, args.package_dir, args.version,
                                    progress=lambda e: events.put(("progress", e)), log=log, already_locked=True,
                                    verify=lambda path: verify_and_restart(path, args.version,
                                        lambda e: events.put(("progress", e)), log))
                    completion = "更新完成，主程序已重新打开并确认就绪。"
                log(completion)
                events.put(("success", completion))
        except BaseException as error:
            message = str(error) or type(error).__name__
            log("更新未完成：" + message)
            if args.ready_file and not args.ready_file.exists():
                _write_json(args.ready_file, {"ready": False, "error": message[:500]})
            events.put(("error", message))
        finally:
            finished.set()

    phases = {"verify": "复核安装包签名和文件", "waiting": "等待原程序退出", "copying": "复制新版文件", "replacing": "替换程序文件",
              "verifying": "检查新版本启动", "restarting": "重新打开主程序"}

    def poll():
        try:
            while True:
                kind, value = events.get_nowait()
                if kind == "log":
                    text.insert("end", value + "\n")
                    text.see("end")
                elif kind == "progress":
                    phase = value["phase"]
                    done, total = value.get("done", 0), max(value.get("total", 1), 1)
                    if phase == "verify":
                        detail = f"{done / 1048576:.1f} / {total / 1048576:.1f} MB"
                    else:
                        unit = " 秒" if phase in ("waiting", "restarting") else " 个文件" if phase == "copying" else " 项"
                        detail = f"{done} / {total}{unit}"
                    title.set(phases.get(phase, phase) + "  " + detail)
                    bar["value"] = min(100, 100 * done / total)
                elif kind == "success":
                    title.set(value)
                    bar["value"] = 100
                    close.configure(state="normal")
                    root.after(2000, root.destroy)
                elif kind == "error":
                    title.set("更新未完成，请查看下方日志")
                    close.configure(state="normal")
        except queue.Empty:
            pass
        root.after(100, poll)

    root.update_idletasks()
    root.after(150, lambda: threading.Thread(target=work, daemon=True).start())
    root.after(100, poll)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
