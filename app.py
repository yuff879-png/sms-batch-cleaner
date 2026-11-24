"""Windows desktop UI for the USB SMS cleaner. No device work runs on Tk's thread."""

from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timedelta, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
import uuid

from version import VERSION

MYANMAR = timezone(timedelta(hours=6, minutes=30))
TYPE_NAMES = {"1": "收到", "2": "已发送", "3": "草稿", "4": "发件箱", "5": "失败", "6": "待发送"}


def application_paths() -> tuple[Path, Path]:
    frozen = bool(getattr(sys, "frozen", False))
    base = Path(sys.executable).resolve().parent if frozen else Path(__file__).resolve().parent
    resources = Path(getattr(sys, "_MEIPASS", base))
    if not frozen and (base.parent / "portable.flag").is_file():
        base = base.parent
    candidates = [base / "data"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "SmsCleaner")
    for candidate in candidates:
        test_path = candidate / (".write-test-" + uuid.uuid4().hex)
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            test_path.write_text("", encoding="utf-8")
            test_path.unlink()
            return candidate, resources
        except OSError:
            continue
    raise OSError("无法创建数据目录。请将工具解压到有写入权限的文件夹再运行。")


def format_types(types: dict) -> str:
    return "，".join(f"{TYPE_NAMES.get(str(kind), '类型 ' + str(kind))} {count:,}" for kind, count in sorted(types.items(), key=lambda item: str(item[0]))) or "没有短信"


def write_startup_report(path: Path, application) -> bool:
    """Called by Tk after entering its event loop; never include device data."""
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump({"ready": application.cleaner is not None, "version": VERSION,
                       "pid": os.getpid()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        return True
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        application.log("[启动确认] 无法写入启动状态：" + type(exc).__name__)
        return False


class CleanerApp:
    def __init__(self, root: tk.Tk, work_dir: Path, resource_dir: Path, smoke_test: bool = False):
        self.root = root
        self.work_dir = work_dir
        self.resource_dir = Path(resource_dir)
        self.portable_install = (self.resource_dir.parent / "portable.flag").is_file()
        self.install_dir = self.resource_dir.parent if self.portable_install else self.resource_dir
        self.events: queue.Queue = queue.Queue()
        self.log_lines: deque[str] = deque(maxlen=5000)
        self.devices: list[dict] = []
        self.checked_serial: str | None = None
        self.plan_result = None
        self.busy = False
        self.operation = ""
        self.stop_requested = False
        self.closing = False
        self.cleaner = None
        self.updater = None
        self.latest_release = None
        self.operation_domain = "phone"
        self.automatic_check_done = False
        self.automatic_check_waits = 0
        self.safe_error_type: type[Exception] = ()
        self.update_error_type: type[Exception] = ()
        self.update_cancel_type: type[Exception] = ()
        self._setup_logger()
        self._build_ui()
        self.root.report_callback_exception = self._callback_error
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(75, self._drain_events)
        self.log(f"短信批量清理工具 v{VERSION} | Windows {platform.release()} | Python {platform.python_version()}")
        self.log("日志时间使用缅甸时间 UTC+06:30。调试日志不包含手机号、短信正文或完整设备序列号。")
        if smoke_test:
            self.log("界面自检模式：没有连接、读取或修改手机。")
            self.root.after(600, self.root.destroy)
            return
        try:
            import core

            self.safe_error_type = core.CleanerError
            self.cleaner = core.Cleaner(work_dir=work_dir, resource_dir=resource_dir, log=self.log, progress=self.progress)
        except Exception as exc:
            detail = str(exc) if isinstance(exc, self.safe_error_type) else type(exc).__name__
            self.log("[错误] 初始化失败：" + detail)
            self.status.set("初始化失败，请复制日志排查")
            messagebox.showerror("无法启动", "初始化失败：" + detail + "\n\n请确认已经完整解压工具包，再点击「一键复制日志」。", parent=root)
        try:
            import updater

            self.update_error_type = getattr(updater, "UpdateError", ())
            self.update_cancel_type = getattr(updater, "UpdateCancelled", ())
            self.updater = updater.Updater(
                install_dir=self.install_dir, resource_dir=self.resource_dir,
                work_dir=self.work_dir, log=self.log, progress=self.update_progress,
            )
            if not self.portable_install:
                self.update_status.set("开发环境可检查版本；请使用发行包安装更新。")
        except Exception as exc:
            detail = str(exc) if isinstance(exc, self.update_error_type) else type(exc).__name__
            self.log("[更新] 更新组件初始化失败：" + detail)
            self.update_status.set("更新组件不可用，请复制日志排查。")
        self._update_controls()
        if self.cleaner is not None:
            self.root.after(150, self.refresh_devices)
        if self.updater is not None:
            self.root.after(2000, self._automatic_update_check)

    def _setup_logger(self):
        logs = self.work_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger("sms_cleaner_ui_" + uuid.uuid4().hex)
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.file_handler = RotatingFileHandler(logs / "sms-cleaner.log", maxBytes=2_000_000, backupCount=4, encoding="utf-8")
        self.file_handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger.addHandler(self.file_handler)

    def _build_ui(self):
        self.root.title(f"短信批量清理工具 v{VERSION}")
        self.root.geometry("1000x850")
        self.root.minsize(850, 720)
        self.root.configure(bg="#f2f5f9")
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Main.TFrame", background="#f2f5f9")
        style.configure("Header.TLabel", font=("Microsoft YaHei UI", 16, "bold"), background="#f2f5f9")
        style.configure("Hint.TLabel", foreground="#526176", background="#f2f5f9")
        style.configure("Card.TLabelframe", padding=8)
        style.configure("TButton", padding=(8, 3))

        container = ttk.Frame(self.root, padding=14, style="Main.TFrame")
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(6, weight=1)
        ttk.Label(container, text="短信批量清理", style="Header.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(container, text="USB 连接手机 → 检测 → 清空短信。多台手机请逐台操作。", style="Hint.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 6))

        device_card = ttk.LabelFrame(container, text="1  选择并检测手机", style="Card.TLabelframe")
        device_card.grid(row=2, column=0, sticky="ew")
        device_card.columnconfigure(0, weight=1)
        self.device_choice = ttk.Combobox(device_card, state="readonly", values=[])
        self.device_choice.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.device_choice.bind("<<ComboboxSelected>>", self._device_changed)
        self.refresh_button = ttk.Button(device_card, text="刷新设备", command=self.refresh_devices)
        self.refresh_button.grid(row=0, column=1, padx=(0, 8))
        self.probe_button = ttk.Button(device_card, text="检测设备", command=self.probe_device)
        self.probe_button.grid(row=0, column=2)
        self.device_info = tk.StringVar(value="请开启 USB 调试，解锁手机，并允许此电脑调试和 root 授权。")
        ttk.Label(device_card, textvariable=self.device_info, wraplength=870).grid(row=1, column=0, columnspan=3, sticky="w", pady=(5, 0))

        action_card = ttk.LabelFrame(container, text="2  清空短信", style="Card.TLabelframe")
        action_card.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self.delete_button = ttk.Button(action_card, text="清空短信…", command=self.delete_sms)
        self.delete_button.grid(row=0, column=0, padx=(0, 10), sticky="w")
        self.stop_button = ttk.Button(action_card, text="停止", command=self.request_stop)
        self.stop_button.grid(row=0, column=1, padx=(0, 10), sticky="w")
        self.plan_info = tk.StringVar(value="请先检测手机。检测完成后，确认设备和数量即可清空。")
        ttk.Label(action_card, textvariable=self.plan_info, wraplength=870).grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 0))
        ttk.Label(action_card, text="范围：本机 SMS（含已发送、失败、草稿等）；不含彩信、SIM 卡短信、RCS 或云端记录。", foreground="#526176", wraplength=870).grid(row=2, column=0, columnspan=2, sticky="w", pady=(3, 0))

        status_frame = ttk.Frame(container, style="Main.TFrame")
        status_frame.grid(row=4, column=0, sticky="ew", pady=(7, 7))
        status_frame.columnconfigure(0, weight=1)
        self.status = tk.StringVar(value="准备就绪")
        ttk.Label(status_frame, textvariable=self.status, style="Hint.TLabel", wraplength=650).grid(row=0, column=0, sticky="w")
        self.progress_bar = ttk.Progressbar(status_frame, mode="indeterminate", length=210)
        self.progress_bar.grid(row=0, column=1, sticky="e", padx=(10, 0))

        update_card = ttk.LabelFrame(container, text="版本更新", style="Card.TLabelframe")
        update_card.grid(row=5, column=0, sticky="ew", pady=(0, 7))
        update_card.columnconfigure(0, weight=1)
        ttk.Label(update_card, text=f"当前版本：v{VERSION}").grid(row=0, column=0, sticky="w")
        self.check_update_button = ttk.Button(update_card, text="检查更新", command=self.check_updates)
        self.check_update_button.grid(row=0, column=1, padx=(8, 0))
        self.install_update_button = ttk.Button(update_card, text="下载并更新…", command=self.download_update)
        self.install_update_button.grid(row=0, column=2, padx=(8, 0))
        self.cancel_update_button = ttk.Button(update_card, text="取消下载", command=self.cancel_update)
        self.cancel_update_button.grid(row=0, column=3, padx=(8, 0))
        self.update_status = tk.StringVar(value="尚未检查更新。")
        ttk.Label(update_card, textvariable=self.update_status, wraplength=500).grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 0))
        self.update_progress_bar = ttk.Progressbar(update_card, mode="determinate")
        self.update_progress_bar.grid(row=1, column=2, columnspan=2, sticky="ew", pady=(5, 0), padx=(8, 0))

        log_card = ttk.LabelFrame(container, text="实时日志", style="Card.TLabelframe")
        log_card.grid(row=6, column=0, sticky="nsew")
        log_card.columnconfigure(0, weight=1)
        log_card.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_card, height=12, state="disabled", wrap="word", background="#172333", foreground="#e0e9f5", insertbackground="white", relief="flat", padx=8, pady=6, font=("Microsoft YaHei UI", 10))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_card, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)
        log_actions = ttk.Frame(log_card)
        log_actions.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(log_actions, text="一键复制日志", command=self.copy_logs).pack(side="left", padx=(0, 10))
        ttk.Button(log_actions, text="打开日志文件夹", command=lambda: self._open_folder("logs")).pack(side="left")
        ttk.Label(log_actions, text="异常时复制日志发给维护人员即可", foreground="#526176").pack(side="right")
        self._update_controls()

    def log(self, message: str):
        timestamp = datetime.now(MYANMAR).strftime("%Y-%m-%d %H:%M:%S")
        text = str(message)
        for device in self.devices:
            serial = str(device.get("serial", ""))
            if serial:
                text = text.replace(serial, "设备…" + serial[-4:])
        # Each event stays readable when copying logs from the interface.
        line = f"[{timestamp}] {text}"
        self.logger.info(line)
        self.events.put(("log", line))

    def progress(self, value: dict):
        self.events.put(("progress", value))

    def update_progress(self, value: dict):
        self.events.put(("update_progress", value))

    def _drain_events(self):
        try:
            for _ in range(250):
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self.log_lines.append(value)
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", value + "\n")
                    if int(self.log_text.index("end-1c").split(".")[0]) > 5500:
                        self.log_text.delete("1.0", "501.0")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif kind == "progress":
                    self._show_progress(value)
                elif kind == "update_progress":
                    self._show_update_progress(value)
                elif kind == "complete":
                    callback, result = value
                    self._set_busy(False)
                    callback(result)
                    if self.closing:
                        return
                elif kind == "error":
                    self._set_busy(False)
                    if value["domain"] == "update":
                        if value["cancelled"]:
                            self.update_status.set("已取消更新，当前版本保持不变。")
                            self.status.set("已取消更新")
                        else:
                            self.update_status.set("更新未完成：" + value["detail"])
                            self.status.set("更新未完成，请查看日志")
                            if not value["quiet"]:
                                messagebox.showerror("更新未完成", value["detail"] + "\n\n点击「一键复制日志」可复制调试信息。", parent=self.root)
                        continue
                    if self.operation in ("检测手机与短信权限", "清空短信"):
                        self._invalidate()
                        self.plan_info.set("本次操作未完成。请查看日志并重新检测手机。")
                        self._update_controls()
                    self.status.set("操作未完成，请查看日志")
                    messagebox.showerror("操作未完成", value["detail"] + "\n\n点击「一键复制日志」可复制调试信息。", parent=self.root)
        except queue.Empty:
            pass
        if not self.closing:
            self.root.after(75, self._drain_events)

    def _show_progress(self, value: dict):
        total = value.get("total")
        processed = value.get("processed", value.get("count", 0))
        phase_names = {
            "snapshot": "正在检测短信", "scanning": "正在统计短信",
            "validating_plan": "正在校验删除计划", "verifying": "正在复查剩余短信",
        }
        prefix = "已请求停止，等待手机确认" if self.stop_requested else phase_names.get(value.get("phase"), self.operation)
        if isinstance(total, (int, float)) and total > 0:
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate", maximum=total, value=processed)
            suffix = f"，已删除 {value['deleted']:,} 条" if "deleted" in value else ""
            self.status.set(f"{prefix}：{processed:,} / {total:,}{suffix}")
        elif isinstance(processed, (int, float)):
            self.status.set(f"{prefix}：已处理 {processed:,} 条")

    def _run(self, operation: str, function, callback, *, domain="phone", quiet=False):
        target = self.updater if domain == "update" else self.cleaner
        if self.busy or target is None:
            return
        self.operation = operation
        self.operation_domain = domain
        self.stop_requested = False
        self._set_busy(True)
        self.status.set(operation + "…")
        self.log("[开始] " + operation)

        def worker():
            try:
                result = function()
                self.events.put(("complete", (callback, result)))
            except Exception as exc:
                safe_type = self.update_error_type if domain == "update" else self.safe_error_type
                cancelled = domain == "update" and isinstance(exc, self.update_cancel_type)
                detail = "更新已取消。" if cancelled else str(exc) if isinstance(exc, safe_type) else f"内部异常 {type(exc).__name__}；请把日志发给维护人员。"
                self.log(("[取消] " if cancelled else "[错误] ") + detail)
                self.events.put(("error", {"detail": detail, "domain": domain, "quiet": quiet, "cancelled": cancelled}))

        threading.Thread(target=worker, daemon=True, name="sms-cleaner-worker").start()

    def _selected_device(self):
        index = self.device_choice.current()
        return self.devices[index] if 0 <= index < len(self.devices) else None

    def _update_controls(self):
        available = self.cleaner is not None and not self.busy
        device = self._selected_device()
        connected = bool(device and device.get("state") == "device")
        checked = connected and device["serial"] == self.checked_serial
        self.device_choice.configure(state="readonly" if available else "disabled")
        self.refresh_button.configure(state="normal" if available else "disabled")
        self.probe_button.configure(state="normal" if available and connected else "disabled")
        deletable = available and checked and self.plan_result is not None and self.plan_result.count > 0 and self.plan_result.serial == device["serial"]
        self.delete_button.configure(state="normal" if deletable else "disabled")
        self.stop_button.configure(state="normal" if self.busy and self.operation in ("检测手机与短信权限", "清空短信") and not self.stop_requested else "disabled")
        update_available = self.updater is not None and not self.busy
        self.check_update_button.configure(state="normal" if update_available else "disabled")
        self.install_update_button.configure(state="normal" if update_available and self.portable_install and self.latest_release is not None else "disabled")
        self.cancel_update_button.configure(state="normal" if self.busy and self.operation_domain == "update" and self.operation == "下载更新" and not self.stop_requested else "disabled")

    def _set_busy(self, busy: bool):
        self.busy = busy
        self.progress_bar.stop()
        self.update_progress_bar.stop()
        if busy:
            active_bar = self.update_progress_bar if self.operation_domain == "update" else self.progress_bar
            active_bar.configure(mode="indeterminate")
            active_bar.start(12)
        else:
            self.progress_bar.configure(mode="determinate", value=0)
        self._update_controls()

    def _invalidate(self):
        self.checked_serial = None
        previous_plan = self.plan_result
        self.plan_result = None
        if previous_plan is not None and self.cleaner is not None:
            self._discard_plan(previous_plan)
        self.plan_info.set("请检测当前手机，再确认清空。")

    def _discard_plan(self, snapshot):
        try:
            self.cleaner.discard_plan(snapshot)
        except Exception as exc:
            detail = str(exc) if isinstance(exc, self.safe_error_type) else type(exc).__name__
            self.log("[临时记录] 本地检测记录未清理：" + detail)

    def _device_changed(self, _event=None):
        self._invalidate()
        self.device_info.set("已切换手机，请点击「检测设备」。")
        self.status.set("等待检测当前手机")
        self._update_controls()

    def refresh_devices(self):
        if self.busy or self.cleaner is None:
            return
        self._invalidate()
        self._run("刷新设备", self.cleaner.list_devices, self._devices_ready)

    def _devices_ready(self, devices: list[dict]):
        self.devices = devices
        states = {"device": "已连接", "unauthorized": "待手机授权", "offline": "离线"}
        labels = [f"{device.get('model') or 'Android 手机'}  ·  尾号 {device['serial'][-4:]}  ·  {states.get(device.get('state'), device.get('state', '未知'))}" for device in devices]
        self.device_choice.configure(values=labels)
        if len(devices) == 1:
            self.device_choice.current(0)
        else:
            self.device_choice.set("")
        if not devices:
            self.device_info.set("未发现手机。请检查 USB 数据线、USB 调试、手机授权和 Windows 驱动，然后刷新。")
            self.status.set("未发现手机")
            self.log("[连接] 未发现设备。请保持手机解锁，检查数据线和 USB 调试授权。")
        else:
            self.device_info.set(f"发现 {len(devices)} 台设备。请选择一台并检测；待授权时请在手机上允许 USB 调试。")
            self.status.set("请选择并检测手机")
            self.log(f"[连接] 发现 {len(devices)} 台设备。")
        self._update_controls()

    def probe_device(self):
        if self.busy or self.cleaner is None:
            return
        device = self._selected_device()
        if not device:
            return
        self._invalidate()
        serial = device["serial"]
        self._run("检测手机与短信权限", lambda: self.cleaner.prepare(serial), self._plan_ready)

    def _plan_ready(self, snapshot):
        device = self._selected_device()
        if not device or snapshot.serial != device["serial"]:
            self._discard_plan(snapshot)
            self._invalidate()
            self.status.set("手机选择已变化，请重新检测")
            self._update_controls()
            return
        self.checked_serial = snapshot.serial
        self.plan_result = snapshot
        model = snapshot.model or "Android 手机"
        android = snapshot.android or "未知"
        count = snapshot.count
        self.device_info.set(f"{model} · Android {android} · 短信 {count:,} 条\n{format_types(snapshot.types)}")
        self.plan_info.set(f"本次检测到 {count:,} 条短信。核对当前手机后，点击「清空短信」。" if count else "当前手机没有短信，无需清空。")
        self.status.set("检测通过，可以清空短信" if count else "当前手机没有短信")
        self.log(f"[检测完成] {model}，Android {android}，短信 {count:,} 条。")
        self._update_controls()

    def delete_sms(self):
        if self.busy or self.cleaner is None:
            return
        snapshot = self.plan_result
        device = self._selected_device()
        if not snapshot or not device or snapshot.serial != device["serial"] or not snapshot.count:
            return
        prompt = (
            f"设备：{snapshot.model or device.get('model') or 'Android 手机'}，尾号 {device['serial'][-4:]}\n"
            f"准备删除：{snapshot.count:,} 条短信\n\n"
            "确认后将直接删除本次检测到的短信。\n"
            "仅处理本次检测的短信编号，之后新增的短信留待下次检测。\n\n"
            "输入「清空」后继续："
        )
        confirmation = simpledialog.askstring("确认清空当前手机", prompt, parent=self.root)
        if confirmation != "清空":
            self.log("[取消] 未确认清空，手机短信未修改。")
            return
        # A deletion plan is single-use in the interface, even on partial failure.
        self.plan_result = None

        def delete_current():
            try:
                return self.cleaner.delete(snapshot)
            finally:
                self._discard_plan(snapshot)

        self._run("清空短信", delete_current, self._delete_ready)

    def _delete_ready(self, result: dict):
        deleted = result.get("deleted", 0)
        missing = result.get("missing", 0)
        changed = result.get("changed", 0)
        failed = result.get("failed", 0)
        remaining = result.get("remaining_target")
        total = result.get("remaining_total")
        cancelled = bool(result.get("cancelled"))
        verified = result.get("verified") is True and remaining == 0 and not cancelled and not failed
        if cancelled:
            title = "已停止清理"
        elif verified:
            title = "本次目标已清空"
        else:
            title = "部分短信尚未清空"
        summary = (
            f"已删除：{deleted:,} 条\n"
            f"原本已不存在：{missing:,} 条\n"
            f"内容发生变化而跳过：{changed:,} 条\n"
            f"删除失败：{failed:,} 条\n"
            f"本次目标剩余：{remaining if remaining is not None else '未确认'}\n"
            f"当前手机短信总数：{total if total is not None else '未确认'}"
        )
        if verified and total:
            summary += "\n\n手机仍有其他或新到短信，需要重新检测后再清理。"
        if not verified:
            summary += "\n\n请查看日志，并重新检测手机确认剩余短信。"
        self.status.set(title)
        self.plan_info.set("本次操作已结束。再次清理前，请重新检测手机。")
        self.checked_serial = None
        self.log(f"[清理结果] {title}；删除 {deleted}，已不存在 {missing}，变化 {changed}，失败 {failed}，目标剩余 {remaining}，总剩余 {total}。")
        self._update_controls()
        if verified:
            messagebox.showinfo(title, summary, parent=self.root)
        else:
            messagebox.showwarning(title, summary, parent=self.root)

    def request_stop(self):
        if not self.busy or not self.cleaner or self.stop_requested or self.operation_domain != "phone":
            return
        self.stop_requested = True
        self.status.set("已请求停止，正在等待手机确认，请保持 USB 连接…")
        self.log("[停止请求] 正在请求停止；尚未收到手机完成或停止结果，请保持 USB 连接。")
        self._update_controls()

        def cancel_worker():
            try:
                self.cleaner.cancel()
            except Exception as exc:
                detail = str(exc) if isinstance(exc, self.safe_error_type) else type(exc).__name__
                self.log("[停止异常] " + detail + "；请等待当前操作返回，不要假定手机已停止。")

        threading.Thread(target=cancel_worker, daemon=True, name="sms-cleaner-cancel").start()

    def _automatic_update_check(self):
        if self.closing or self.updater is None or self.automatic_check_done:
            return
        if self.busy:
            self.automatic_check_waits += 1
            if self.automatic_check_waits <= 30:
                self.root.after(2000, self._automatic_update_check)
            else:
                self.automatic_check_done = True
                self.update_status.set("操作期间已跳过自动检查；空闲时可点击「检查更新」。")
            return
        self.check_updates(automatic=True)

    def check_updates(self, automatic=False):
        if self.busy or self.updater is None:
            return
        self.automatic_check_done = True
        self.latest_release = None
        self.update_status.set("正在检查最新版本…")
        self._run("检查更新", lambda: self.updater.check(VERSION), self._update_checked,
                  domain="update", quiet=automatic)

    def _update_checked(self, release):
        self.latest_release = release
        if release is None:
            self.update_status.set(f"当前 v{VERSION} 已是最新版本。")
            self.log("[更新] 当前版本已是最新版本。")
        else:
            suffix = "点击「下载并更新」安装。" if self.portable_install else "开发环境不执行安装，请使用发行包更新。"
            self.update_status.set(f"发现新版本 v{release.version}。{suffix}")
            self.log(f"[更新] 发现新版本 v{release.version}。")
        self.status.set("更新检查完成")
        self._update_controls()

    def download_update(self):
        if self.busy or self.updater is None or self.latest_release is None or not self.portable_install:
            return
        release = self.latest_release
        notes = str(getattr(release, "notes", "") or "")[:1800]
        prompt = f"准备从 v{VERSION} 更新到 v{release.version}。\n\n下载、校验完成后，当前工具将退出，并由独立更新窗口显示安装进度。完成后重新打开工具。"
        if notes:
            prompt += "\n\n更新说明：\n" + notes
        if not messagebox.askyesno("下载并更新", prompt, parent=self.root):
            return
        self._invalidate()
        self.update_progress_bar.configure(mode="determinate", value=0)
        self.update_status.set(f"正在准备下载 v{release.version}…")
        self._run("下载更新", lambda: self.updater.download(release), self._update_downloaded, domain="update")

    def _update_downloaded(self, prepared):
        if self.stop_requested:
            self.update_status.set("已取消更新，当前版本保持不变。")
            self.status.set("已取消更新")
            self.log("[更新] 安装尚未启动；取消请求已生效。")
            self._update_controls()
            return
        self.update_status.set("下载与校验完成，正在启动独立更新窗口…")
        self._run("启动更新", lambda: self.updater.launch_install(prepared, os.getpid()),
                  self._installer_started, domain="update")

    def _installer_started(self, _result):
        self.log("[更新] 独立更新窗口已就绪，正在退出主界面。")
        self._set_busy(False)
        self._close()

    def _show_update_progress(self, value: dict):
        phases = {"checking": "正在检查更新", "downloading": "正在下载", "download": "正在下载",
                  "verifying": "正在校验", "verify": "正在校验", "extracting": "正在解压",
                  "extract": "正在解压", "ready": "更新文件已就绪"}
        phase = phases.get(value.get("phase"), "正在准备更新")
        processed = value.get("processed", value.get("done", value.get("downloaded", 0)))
        total = value.get("total")
        if isinstance(total, (int, float)) and total > 0 and isinstance(processed, (int, float)):
            self.update_progress_bar.stop()
            self.update_progress_bar.configure(mode="determinate", maximum=total, value=processed)
            phase += f"：{min(100, processed / total * 100):.0f}%"
            speed = value.get("speed")
            if value.get("phase") in ("download", "downloading") and isinstance(speed, (int, float)) and speed > 0:
                phase += f" · {speed / 1024 / 1024:.1f} MB/s"
        else:
            self.update_progress_bar.stop()
            self.update_progress_bar.configure(mode="indeterminate")
            self.update_progress_bar.start(12)
        if self.stop_requested:
            phase = "正在取消更新，请等待当前步骤结束…"
        self.update_status.set(phase)

    def cancel_update(self):
        if not self.busy or self.operation_domain != "update" or self.operation != "下载更新" or self.stop_requested:
            return
        self.stop_requested = True
        self.update_status.set("正在取消更新，请等待当前步骤结束…")
        self.log("[更新] 已请求取消下载或解压；安装尚未启动。")
        self._update_controls()

        def cancel_worker():
            try:
                self.updater.cancel()
            except Exception as exc:
                detail = str(exc) if isinstance(exc, self.update_error_type) else type(exc).__name__
                self.log("[更新] 取消请求失败：" + detail)

        threading.Thread(target=cancel_worker, daemon=True, name="sms-cleaner-update-cancel").start()

    def copy_logs(self):
        # Copy the visible session log, including its version and time-zone context.
        content = f"短信批量清理工具 v{VERSION}\nWindows {platform.release()} | Python {platform.python_version()}\n时间：缅甸 UTC+06:30\n\n" + "\n".join(self.log_lines)
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        self.root.update_idletasks()
        self.log("[日志] 已复制界面日志，可粘贴发送给维护人员。")

    def _open_folder(self, name: str):
        folder = self.work_dir / name
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(folder)
        except OSError as exc:
            self.log(f"[错误] 无法打开文件夹：{type(exc).__name__}")
            messagebox.showerror("无法打开文件夹", f"请手动打开：\n{folder}", parent=self.root)

    def _callback_error(self, exc_type, _exc_value, _traceback):
        self.log("[界面异常] " + exc_type.__name__ + "；请复制日志发给维护人员。")

    def _close(self):
        if self.busy:
            if self.operation_domain == "update":
                messagebox.showinfo("正在更新", "更新步骤尚未结束。下载或解压时可点击「取消下载」，等待结果后再退出。", parent=self.root)
            else:
                messagebox.showinfo("正在操作手机", "操作尚未结束。需要停止时，请先点击「停止」，并等待结果后再退出。", parent=self.root)
            return
        self._invalidate()
        self.closing = True
        self.progress_bar.stop()
        self.update_progress_bar.stop()
        for timer in self.root.tk.call("after", "info"):
            self.root.after_cancel(timer)
        self.file_handler.close()
        self.logger.removeHandler(self.file_handler)
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="USB 短信批量清理工具")
    parser.add_argument("--smoke-test", action="store_true", help="检查界面能否创建，不操作手机")
    parser.add_argument("--startup-report", type=Path, help="更新完成后写入启动确认")
    args = parser.parse_args()
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    try:
        work_dir, resource_dir = application_paths()
        application = CleanerApp(root, work_dir, resource_dir, smoke_test=args.smoke_test)
    except OSError as exc:
        root.withdraw()
        messagebox.showerror("无法启动", str(exc), parent=root)
        root.destroy()
        return 1
    if args.startup_report and not args.smoke_test:
        root.after(250, lambda: write_startup_report(args.startup_report, application))
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
