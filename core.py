"""USB SMS cleanup using temporary ID plans, without saving message content."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable

from version import VERSION


class CleanerError(RuntimeError):
    """Contains only text safe to show or copy into diagnostic logs."""


@dataclass(frozen=True)
class Snapshot:
    path: Path
    count: int
    types: dict
    serial: str
    sha256: str
    model: str
    android: str


def fingerprint(record: dict) -> str:
    digest = hashlib.sha256()
    for key in ("_id", "date", "address", "body", "type", "thread_id"):
        value = record.get(key)
        if value is None:
            digest.update(b"-1:")
        else:
            data = str(value).encode("utf-8")
            digest.update(str(len(data)).encode("ascii") + b":" + data)
    return digest.hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Cleaner:
    def __init__(self, work_dir: Path, resource_dir: Path,
                 log: Callable[[str], None], progress=None):
        self.work_dir = Path(work_dir)
        self.resource_dir = Path(resource_dir)
        self.log = log
        self.progress = progress or (lambda event: None)
        self._cancel = threading.Event()
        self._bridge_dirs: dict[str, str] = {}
        self._probed: set[str] = set()
        candidates = [self.resource_dir / "platform-tools" / "adb.exe",
                      self.resource_dir / "adb.exe"]
        external = shutil.which("adb")
        if external:
            candidates.append(Path(external))
        self.adb = next((p for p in candidates if p.is_file()), None)
        if self.adb is None:
            raise CleanerError("找不到 ADB。请完整解压工具包，保留 platform-tools 文件夹。")
        self.bridge = self.resource_dir / "bridge.jar"
        if not self.bridge.exists():
            self.bridge = self.resource_dir / "android" / "build" / "bridge.jar"

    def cancel(self):
        self._cancel.set()

    @staticmethod
    def _sms_modes(result):
        return ", ".join(f"{key}={result[key] if type(result.get(key)) is int else 'unknown'}"
                         for key in ("read_sms_mode", "write_sms_mode"))

    @staticmethod
    def _device_diagnostic(result, serial):
        model = result.get("model")
        model = model if isinstance(model, str) else "未知"
        if serial:
            model = model.replace(serial, "[设备]")
        model = re.sub(r"[^A-Za-z0-9\u3400-\u9fff ._()\-]", "_", model)[:64]
        model = re.sub(r"\d{7,}", "[数字已隐藏]", model) or "未知"
        suffix = re.sub(r"[^A-Za-z0-9]", "_", serial[-4:]) if len(serial) > 4 else "隐藏"
        return f"model={model} serial_suffix=…{suffix}"

    def _validate_sms_summary(self, result, serial):
        if not isinstance(result, dict):
            raise CleanerError("手机未返回有效的短信检测结果，当前短信数量未知；已阻止清空。")
        modes = self._sms_modes(result)
        self.log("短信能力：" + self._device_diagnostic(result, serial) + " " + modes)
        if type(result.get("uid")) is not int or result["uid"] != 1001:
            raise CleanerError("短信接口身份不正确，当前短信数量未知；已阻止后续操作。")
        if type(result.get("read_sms_mode")) is not int or result["read_sms_mode"] != 0:
            raise CleanerError("短信读取受限或权限未确认，当前短信数量未知。请检查系统短信读取权限及 root/ROM 限制，"
                               f"重新检测后再清理。（{modes}）")
        if type(result.get("write_sms_mode")) is not int or result["write_sms_mode"] != 0:
            raise CleanerError("系统未确认短信清理权限，已阻止清空。请检查系统短信权限及 root/ROM 限制。"
                               f"（{modes}）")
        count = result.get("count")
        if type(count) is not int or count < 0:
            raise CleanerError("手机返回的短信数量无效，当前短信数量未知；请重新检测，已阻止清空。")
        return count

    @staticmethod
    def _command_failure(message: str, exit_code: int, stage: str):
        """Classify known diagnostics without returning their private contents."""
        lines = [line.strip().lower() for line in message.splitlines()]
        prefix = r"^(?:adb:\s*)?(?:error:\s*)?"
        device = r"device(?:\s+(?:'[^']*'|\"[^\"]*\"))?"
        if any(re.search(prefix + device + r"\s+unauthorized\b", line) for line in lines):
            return "ADB_UNAUTHORIZED", "手机尚未授权 USB 调试，请在手机上允许此电脑。"
        if any(re.search(prefix + device + r"\s+offline\b", line) for line in lines):
            return "ADB_OFFLINE", "设备已离线，请检查数据线并重新检测设备。"
        if any(re.search(prefix + r"(?:" + device + r"\s+not found|no devices(?:/emulators)? found)\b", line) for line in lines):
            return "ADB_DEVICE_MISSING", "未找到已选择的手机，请检查 USB 连接并刷新设备列表。"

        text = "\n".join(lines)
        denied = "permission denied" in text or "operation not permitted" in text
        if stage == "root_check":
            if exit_code == 127:
                return "ROOT_UNAVAILABLE", "未检测到可用 root（su 不可用）。请在已 root 的手机上使用；若已 root，请检查 ADB/shell 授权或隐藏设置。"
            if exit_code == 126 or denied or "request rejected" in text or "access denied" in text:
                return "ROOT_DENIED", "root 访问被拒绝或无法执行。请检查 root 管理器对 ADB/shell 的授权及限制。"
            if re.search(r"\bsu\b[^\n]*(?:not found|inaccessible)", text):
                return "ROOT_UNAVAILABLE", "未检测到可用 root（su 不可用）。请在已 root 的手机上使用；若已 root，请检查 ADB/shell 授权或隐藏设置。"
            return "ROOT_COMMAND_FAILED", "root 检测命令未成功。请检查 root 管理器状态；此手机的 su 实现可能不兼容。"
        if stage == "current_user":
            if exit_code == 127 or "not found" in text or "inaccessible" in text:
                return "USER_COMMAND_UNAVAILABLE", "无法运行手机用户检测命令；此系统的 Android 命令环境可能不兼容。"
            return "USER_QUERY_FAILED", "无法读取当前 Android 用户。请检查系统权限及 ROM 兼容性。"
        if stage in ("bridge_prepare", "bridge_upload", "plan_upload", "cancel_request", "cleanup"):
            if denied:
                return "FILE_PERMISSION_DENIED", "手机临时文件操作被拒绝。请检查目标目录权限及系统限制。"
            if "no such file" in text or "not found" in text:
                return "FILE_UNAVAILABLE", "文件操作所需的文件或命令不可用，请检查工具完整性并重新检测手机。"
            return "FILE_OPERATION_FAILED", "手机临时文件准备或传输失败，请检查剩余空间、目录权限和系统兼容性。"
        if denied:
            return "COMMAND_PERMISSION_DENIED", "当前手机命令被系统拒绝，请检查相应权限及系统限制。"
        if exit_code == 127 or "not found" in text or "inaccessible" in text:
            return "COMMAND_UNAVAILABLE", "手机端所需命令或文件不可用，请检查系统兼容性。"
        return "ADB_COMMAND_FAILED", "ADB 命令未成功，请根据日志中的操作阶段检查连接和系统状态。"

    def _command(self, args, *, serial=None, timeout=30, stage=None) -> str:
        safe_stage = stage if stage in {
            "devices", "connection", "current_user", "root_check", "boot_check",
            "bridge_prepare", "bridge_upload", "plan_upload", "cancel_request", "cleanup",
        } else "command"

        def record(code, exit_code):
            self.log(f"ADB 阶段：stage={safe_stage} code={code} exit={exit_code}；未记录原始命令及输出。")

        command = [str(self.adb)]
        if serial is not None:
            command += ["-s", serial]
        command += list(args)
        try:
            result = subprocess.run(command, capture_output=True, encoding="utf-8",
                                    errors="replace", timeout=timeout,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            record("ADB_TIMEOUT", "timeout")
            reason = "root 检测超时，请查看手机上的 root 授权提示。" if safe_stage == "root_check" else "ADB 操作超时。请检查 USB 连接，并查看手机上的授权提示。"
            raise CleanerError(f"{reason}（阶段 {safe_stage}）") from None
        except OSError:
            record("ADB_LAUNCH_FAILED", "not_started")
            raise CleanerError(f"无法启动 ADB，请检查工具是否完整解压、是否被安全软件拦截。（阶段 {safe_stage}）") from None
        if result.returncode:
            code, reason = self._command_failure(result.stdout + result.stderr, result.returncode, safe_stage)
            record(code, result.returncode)
            raise CleanerError(f"{reason}（阶段 {safe_stage}，代码 {code}，退出码 {result.returncode}）")
        record("OK", result.returncode)
        return result.stdout

    def list_devices(self):
        output = self._command(["devices", "-l"], stage="devices")
        devices = []
        for line in output.splitlines():
            words = line.split()
            if len(words) < 2 or words[0] in ("List", "*"):
                continue
            if words[1] not in ("device", "unauthorized", "offline", "recovery", "sideload"):
                continue
            model = next((x[6:] for x in words[2:] if x.startswith("model:")), "未知型号")
            devices.append(dict(serial=words[0], state=words[1], model=model))
        self.log(f"设备列表：发现 {len(devices)} 台设备。")
        return devices

    def _ready(self, serial):
        state = self._command(["get-state"], serial=serial, stage="connection").strip()
        if state != "device":
            raise CleanerError("设备当前不可用，请重新连接并授权 USB 调试。")
        current_user = self._command(["shell", "am get-current-user"], serial=serial, stage="current_user").strip()
        if current_user != "0":
            raise CleanerError("当前处于手机分身或其他用户，请切换到手机主用户后操作。")
        marker = self._state_path(serial)
        if marker.exists():
            boot_id = self._boot_id(serial)
            try:
                previous = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise CleanerError("上次删除状态记录损坏。请联系维护人员确认手机已停止清理。") from None
            if previous.get("boot_id") == boot_id:
                raise CleanerError("上次清理未确认结束，为防止重复操作已锁定此设备。请重启手机，再连接并检测。")
            marker.unlink()
            self._bridge_dirs.pop(serial, None)
            self.log("已确认手机重新启动，解除上次未确认清理的锁定。")
        identity = self._command(["shell", "su -c id"], serial=serial, timeout=45, stage="root_check")
        if not re.search(r"\buid=0\b", identity):
            self.log("ADB 阶段：stage=root_check code=ROOT_IDENTITY_MISMATCH exit=0；未记录原始身份输出。")
            raise CleanerError("root 检测未取得 UID 0。请检查 root 管理器对 ADB/shell 的授权。（阶段 root_check，代码 ROOT_IDENTITY_MISMATCH）")
        if not self.bridge.is_file():
            raise CleanerError("缺少 bridge.jar，请重新解压完整工具包。")
        remote = self._bridge_dirs.get(serial)
        if not remote:
            remote = "/data/local/tmp/sms-cleaner-" + secrets.token_hex(10)
            self._command(["shell", f"mkdir -m 755 {shlex.quote(remote)}"], serial=serial, stage="bridge_prepare")
            self._command(["push", str(self.bridge), remote + "/bridge.jar"], serial=serial, stage="bridge_upload")
            self._command(["shell", f"chmod 444 {shlex.quote(remote + '/bridge.jar')}"], serial=serial, stage="bridge_prepare")
            self._bridge_dirs[serial] = remote
        return remote

    def _state_path(self, serial):
        token = hashlib.sha256(serial.encode()).hexdigest()
        return self.work_dir / "state" / (token + ".json")

    def _boot_id(self, serial):
        boot_id = self._command(["shell", "cat /proc/sys/kernel/random/boot_id"], serial=serial, stage="boot_check").strip()
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", boot_id):
            raise CleanerError("无法识别手机启动状态，已阻止清空。")
        return boot_id

    def _run_bridge(self, serial, action, on_record=None, plan=None):
        remote = self._ready(serial)
        cancel_path = remote + "/cancel-" + secrets.token_hex(6)
        remote_plan = None
        bridge_args = [action]
        if plan:
            remote_plan = remote + "/plan-" + secrets.token_hex(6) + ".jsonl"
            self._command(["push", str(plan), remote_plan], serial=serial, stage="plan_upload")
            bridge_args += [remote_plan, cancel_path]
        inner = "CLASSPATH=" + shlex.quote(remote + "/bridge.jar")
        inner += " app_process /system/bin SmsBridge " + " ".join(map(shlex.quote, bridge_args))
        remote_command = "su -c " + shlex.quote(inner)
        command = [str(self.adb), "-s", serial, "shell", remote_command]
        marker = None
        if action == "delete":
            boot_id = self._boot_id(serial)
            marker = self._state_path(serial)
            marker.parent.mkdir(parents=True, exist_ok=True)
            with marker.open("x", encoding="utf-8") as stream:
                json.dump({"boot_id": boot_id, "state": "awaiting_confirmation"}, stream)
                stream.flush()
                os.fsync(stream.fileno())
        try:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    encoding="utf-8", errors="replace",
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            if marker:
                marker.unlink(missing_ok=True)
            raise CleanerError("无法启动手机桥接程序，未开始清空。") from None
        events = queue.Queue()

        def read_lines():
            try:
                for line in proc.stdout:
                    events.put(line)
            finally:
                events.put(None)

        reader = threading.Thread(target=read_lines, daemon=True)
        reader.start()
        summary = None
        completed = False
        last_activity = time.monotonic()
        last_logged_progress = 0.0
        stop_sent_at = None
        try:
            while True:
                now = time.monotonic()
                if self._cancel.is_set() and stop_sent_at is None:
                    if action == "delete":
                        self._command(["shell", "touch " + shlex.quote(cancel_path)], serial=serial, stage="cancel_request")
                        stop_sent_at = now
                        self.log("已向手机发送停止请求，正在等待手机确认；请保持 USB 连接。")
                    else:
                        proc.terminate()
                        raise CleanerError("读取已取消，未执行删除。")
                if stop_sent_at and now - stop_sent_at > 45:
                    raise CleanerError("停止请求已发送，但尚未收到手机确认。当前删除状态未知；请保持连接并重新检测。")
                if now - last_activity > 180:
                    if action == "delete":
                        self._command(["shell", "touch " + shlex.quote(cancel_path)], serial=serial, stage="cancel_request")
                        raise CleanerError("手机长时间未返回进度，已请求停止。不能确认停止结果，请重新检测剩余数量。")
                    raise CleanerError("读取超时，请检查手机 root 授权及 USB 连接。")
                try:
                    line = events.get(timeout=0.15)
                except queue.Empty:
                    continue
                if line is None:
                    break
                last_activity = time.monotonic()
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    # ART/su diagnostics may contain device data. Never copy raw output to logs.
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("event")
                if kind == "error":
                    self.log("短信接口权限诊断：" + self._sms_modes(event))
                    if event.get("code") == "read_not_allowed":
                        self._probed.discard(serial)
                        raise CleanerError("短信读取受限，当前短信数量未知。请检查系统短信读取权限及 root/ROM 限制，"
                                           "重新检测后再清理。"
                                           f"（code=read_not_allowed，{self._sms_modes(event)}）")
                    code = str(event.get("code", "BRIDGE_ERROR"))
                    code = re.sub(r"[^A-Za-z0-9_.$-]", "", code)[:80]
                    exception = re.sub(r"[^A-Za-z0-9_.$-]", "", str(event.get("exception_class", event.get("exception", ""))))[:100]
                    stage = re.sub(r"[^A-Za-z0-9_.$-]", "", str(event.get("stage", "")))[:80]
                    hint = re.sub(r"[^A-Za-z0-9_.$-]", "", str(event.get("hint", "")))[:80]
                    classification = event.get("classification", {})
                    if isinstance(classification, dict):
                        hint += ",".join(re.sub(r"[^a-zA-Z0-9_]", "", str(k))[:30] for k, v in classification.items() if v is True)
                    raise CleanerError(f"手机短信接口执行失败：code={code} stage={stage} exception={exception} hint={hint}。请检查 root 授权和系统兼容性。")
                if kind == "target" and on_record:
                    on_record(event)
                elif kind in ("progress", "status"):
                    self.progress(event)
                    if action == "delete" and (time.monotonic() - last_logged_progress >= 2
                                               or event.get("processed") == event.get("total")):
                        self.log("清理进度：" + ", ".join(f"{key}={event.get(key, '?')}" for key in
                                 ("processed", "total", "deleted", "changed", "missing", "failed")))
                        last_logged_progress = time.monotonic()
                elif kind == "summary":
                    if summary is not None:
                        raise CleanerError("手机返回了重复结果，操作状态需重新核实。")
                    summary = event
            try:
                exit_code = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                raise CleanerError("手机进程未正常退出，请重新检测设备。") from None
            if exit_code or summary is None:
                suffix = "删除可能已部分执行，请重新检测；不要直接重试。" if action == "delete" else "请检查 root 授权与系统兼容性。"
                raise CleanerError(f"手机未返回完整结果（退出码 {exit_code}）。{suffix}")
            if action == "delete" and (summary.get("verified") is not True or
                                        not isinstance(summary.get("remaining_target"), int) or
                                        not isinstance(summary.get("remaining_total"), int)):
                raise CleanerError("手机未返回有效的删除复查结果。已锁定再次清理，请重启手机后重新检测。")
            completed = True
            if marker:
                marker.unlink(missing_ok=True)
            return summary
        finally:
            if proc.poll() is None:
                if action == "delete":
                    try:
                        self._command(["shell", "touch " + shlex.quote(cancel_path)], serial=serial, timeout=5, stage="cancel_request")
                    except CleanerError:
                        self.log("无法确认手机停止状态；请重新连接后检测剩余数量。")
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            reader.join(timeout=2)
            if proc.stdout:
                proc.stdout.close()
            # Plans contain only IDs and fingerprints, never message bodies. Remove after completion.
            if remote_plan and completed:
                try:
                    self._command(["shell", "rm -f " + shlex.quote(remote_plan) + " " + shlex.quote(cancel_path)], serial=serial, timeout=5, stage="cleanup")
                except CleanerError:
                    self.log("临时删除计划未清理，内容仅含记录编号和校验值，不含短信正文。")

    def probe(self, serial):
        self._cancel.clear()
        self._probed.discard(serial)
        self.log("开始只读检测：检查 USB、主用户、root 和短信服务访问能力。")
        result = self._run_bridge(serial, "probe")
        count = self._validate_sms_summary(result, serial)
        self._probed.add(serial)
        self.log(f"检测成功：本机 SMS 共 {count} 条。")
        return result

    def prepare(self, serial):
        self._cancel.clear()
        info = self.probe(serial)
        folder = self.work_dir / "plans"
        folder.mkdir(parents=True, exist_ok=True)
        token = hashlib.sha256(serial.encode()).hexdigest()[:10]
        path = folder / ("plan-" + token + "-" + secrets.token_hex(8) + ".jsonl")
        ids = set()
        self.log("正在统计待清理短信；仅生成临时编号和校验值，不保存短信正文。")
        try:
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps({"event": "metadata", "format": "sms-cleaner-plan-v1",
                                         "device_hash": token}) + "\n")

                def consume(event):
                    message_id = event.get("id")
                    digest = event.get("fingerprint")
                    if (event.get("event") != "target" or not isinstance(message_id, str)
                            or not re.fullmatch(r"[1-9][0-9]{0,18}", message_id)
                            or message_id in ids or not isinstance(digest, str)
                            or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                        raise CleanerError("短信清理计划格式或记录编号异常，已阻止清空。")
                    ids.add(message_id)
                    stream.write(json.dumps({"event": "target", "id": message_id,
                                             "fingerprint": digest}) + "\n")
                    if len(ids) % 200 == 0:
                        self.progress({"event": "progress", "phase": "scanning", "processed": len(ids)})

                summary = self._run_bridge(serial, "scan", consume)
                if self._validate_sms_summary(summary, serial) != len(ids):
                    raise CleanerError("短信统计与计划数量不一致，已阻止清空。")
                stream.write(json.dumps(summary) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            self.log(f"检测完成：本次可清理 {len(ids)} 条 SMS，无需备份。")
            return Snapshot(path, len(ids), summary.get("types", {}), serial,
                            file_hash(path), info.get("model", "Android"), str(info.get("android", "?")))
        except Exception:
            self._probed.discard(serial)
            path.unlink(missing_ok=True)
            raise

    def discard_plan(self, snapshot):
        if isinstance(snapshot, Snapshot):
            path = snapshot.path.resolve()
            if path.parent == (self.work_dir / "plans").resolve() and path.name.startswith("plan-"):
                path.unlink(missing_ok=True)

    def _make_plan(self, snapshot: Snapshot, plan_path: Path):
        try:
            if file_hash(snapshot.path) != snapshot.sha256:
                raise CleanerError("临时清理计划已变化，已阻止删除。请重新检测。")
            ids = set()
            metadata = None
            summary = None
            with snapshot.path.open(encoding="utf-8") as source, plan_path.open("w", encoding="utf-8", newline="\n") as target:
                for line in source:
                    item = json.loads(line)
                    if item.get("event") == "metadata":
                        if metadata is not None or ids:
                            raise CleanerError("清理计划结构无效。")
                        metadata = item
                    elif item.get("event") == "target":
                        message_id = item.get("id", "")
                        digest = item.get("fingerprint", "")
                        if (summary is not None or not isinstance(message_id, str)
                                or not re.fullmatch(r"[1-9][0-9]{0,18}", message_id)
                                or message_id in ids or not isinstance(digest, str)
                                or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                            raise CleanerError("临时清理计划损坏，已阻止删除。请重新检测。")
                        ids.add(message_id)
                        target.write(json.dumps({"id": message_id, "fingerprint": digest}) + "\n")
                    elif item.get("event") == "summary":
                        if summary is not None:
                            raise CleanerError("清理计划结构无效。")
                        summary = item
                    else:
                        raise CleanerError("清理计划包含未知记录。")
            expected_device = hashlib.sha256(snapshot.serial.encode()).hexdigest()[:10]
            if not metadata or metadata.get("device_hash") != expected_device or not summary or summary.get("count") != len(ids) or len(ids) != snapshot.count:
                raise CleanerError("清理计划数量或设备不匹配，已阻止删除。")
            if file_hash(snapshot.path) != snapshot.sha256:
                raise CleanerError("读取过程中清理计划发生变化，已阻止删除。")
        except CleanerError:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            raise CleanerError("无法完整读取清理计划，已阻止删除。") from None

    def delete(self, snapshot: Snapshot):
        if snapshot.serial not in self._probed:
            raise CleanerError("此设备尚未检测，不能清空短信。")
        self._cancel.clear()
        if snapshot.count == 0:
            raise CleanerError("当前没有待清理短信，无需清空。")
        if snapshot.count > 500000:
            raise CleanerError("本版单次清理上限为 50 万条，当前数量超过验证上限，请联系维护人员分批处理。")
        with tempfile.TemporaryDirectory(prefix="sms-cleaner-plan-") as temp:
            plan = Path(temp) / "plan.jsonl"
            self._make_plan(snapshot, plan)
            if self._cancel.is_set():
                raise CleanerError("操作已取消，未开始删除。")
            self.log(f"开始直接清空本次检测的 {snapshot.count} 条 SMS；仅处理本次检测的短信编号。")
            try:
                result = self._run_bridge(snapshot.serial, "delete", plan=plan)
            finally:
                self.discard_plan(snapshot)
        self.log("手机删除结果：" + ", ".join(f"{k}={result.get(k, '?')}" for k in
                 ("total", "deleted", "changed", "missing", "failed", "remaining_target", "remaining_total", "cancelled")))
        if result.get("remaining_target") is None or result.get("remaining_total") is None:
            raise CleanerError("删除执行后缺少复查结果，不能确认已清空，请重新检测。")
        return result
