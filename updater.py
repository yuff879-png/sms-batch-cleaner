"""Public GitHub Releases updater for the portable Windows distribution."""
from __future__ import annotations

import ast
import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

UPDATER_VERSION = "1.1.0"
MAX_DOWNLOAD = 256 * 1024 * 1024
MAX_EXTRACTED = 512 * 1024 * 1024
MAX_FILES = 10000
ROOT_FILES = {"README.md", "CHANGELOG.md", "使用说明.txt", "启动短信清理工具.cmd", "portable.flag"}
REQUIRED_FILES = {
    "app/app.py", "app/core.py", "app/version.py", "app/updater.py",
    "app/update_worker.py", "app/update-channel.json", "app/bridge.jar", "app/platform-tools/adb.exe",
    "runtime/python.exe", "runtime/pythonw.exe", "启动短信清理工具.cmd", "portable.flag",
}
_REDIRECT_HOSTS = {"api.github.com", "github.com", "objects.githubusercontent.com",
                   "release-assets.githubusercontent.com"}


class UpdateError(RuntimeError):
    pass


class UpdateCancelled(UpdateError):
    pass


def version_tuple(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", value):
        raise UpdateError("版本号格式不正确。")
    return tuple(int(part) for part in value.split("."))


def _safe_https(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    return (parsed.scheme == "https" and parsed.hostname in _REDIRECT_HOSTS
            and parsed.port in (None, 443) and not parsed.username and not parsed.password)


class _GitHubRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _safe_https(newurl):
            raise UpdateError("更新下载被重定向到不受信任的地址。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(frozen=True)
class Release:
    version: str
    notes: str
    manifest_url: str
    package_url: str
    size: int
    sha256: str
    tag: str
    repository: str
    signed_manifest: dict | None = None


@dataclass(frozen=True)
class PreparedUpdate:
    stage_dir: Path
    package_dir: Path
    version: str


def validate_package(package: Path, expected_version: str) -> None:
    """Static validation only; never execute downloaded Python while checking it."""
    for relative in REQUIRED_FILES:
        if not (package / relative).is_file():
            raise UpdateError(f"更新包缺少必要文件：{relative}")
    try:
        source = (package / "app/version.py").read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        versions = [node.value.value for node in tree.body
                    if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                    and any(isinstance(t, ast.Name) and t.id == "VERSION" for t in node.targets)]
    except (OSError, SyntaxError, UnicodeError) as error:
        raise UpdateError("无法读取更新包版本。") from error
    if versions != [expected_version]:
        raise UpdateError("更新包内版本与发布清单不一致。")
    try:
        channel = json.loads((package / "app/update-channel.json").read_text(encoding="utf-8-sig"))
        if (not isinstance(channel, dict) or type(channel.get("schema")) is not int
                or channel["schema"] != 1 or not isinstance(channel.get("repository"), str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", channel["repository"])
                or not isinstance(channel.get("public_key"), str)
                or len(base64.b64decode(channel["public_key"], validate=True)) != 32):
            raise ValueError()
    except (OSError, UnicodeError, ValueError, TypeError):
        raise UpdateError("更新包中的更新渠道或签名公钥无效，已拒绝安装。") from None
    for binary in ("runtime/python.exe", "runtime/pythonw.exe", "app/platform-tools/adb.exe"):
        with (package / binary).open("rb") as source:
            if source.read(2) != b"MZ":
                raise UpdateError(f"更新包中的 Windows 程序无效：{binary}")


class Updater:
    def __init__(self, install_dir: Path, resource_dir: Path, work_dir: Path,
                 log=lambda message: None, progress=lambda event: None):
        self.install_dir = Path(install_dir).resolve()
        self.resource_dir = Path(resource_dir).resolve()
        self.work_dir = Path(work_dir).resolve()
        self.log = log
        self.progress = progress
        self._cancel = threading.Event()
        self._opener = urllib.request.build_opener(_GitHubRedirects())

    def cancel(self) -> None:
        self._cancel.set()

    def _check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise UpdateCancelled("已取消更新下载。")

    def _channel(self) -> dict:
        try:
            config = json.loads((self.resource_dir / "update-channel.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as error:
            raise UpdateError("未配置更新渠道，请使用正式发布的便携版。") from error
        repository = config.get("repository")
        if config.get("schema") != 1 or not isinstance(repository, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", repository):
            raise UpdateError("更新渠道配置无效。")
        return config

    def _repository(self) -> str:
        return self._channel()["repository"]

    def _signed_manifest(self, envelope: dict) -> dict:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            from cryptography.exceptions import InvalidSignature
        except ImportError:
            raise UpdateError("更新签名验证组件缺失，请重新下载正式便携包。") from None
        try:
            key = base64.b64decode(self._channel()["public_key"], validate=True)
            payload = base64.b64decode(envelope["payload"], validate=True)
            signature = base64.b64decode(envelope["signature"], validate=True)
            if len(key) != 32 or len(signature) != 64 or len(payload) > 48 * 1024:
                raise ValueError()
            Ed25519PublicKey.from_public_bytes(key).verify(signature, payload)
            manifest = json.loads(payload)
            if not isinstance(manifest, dict):
                raise ValueError()
            return manifest
        except (KeyError, TypeError, ValueError, InvalidSignature):
            raise UpdateError("更新清单签名无效，已拒绝安装。") from None

    def _open(self, url: str):
        self._check_cancelled()
        if not _safe_https(url):
            raise UpdateError("更新请求地址无效。")
        request = urllib.request.Request(url, headers={
            "User-Agent": "SmsCleaner-Updater/" + UPDATER_VERSION,
            "Accept": "application/vnd.github+json" if url.startswith("https://api.github.com/") else "application/octet-stream",
        })
        try:
            return self._opener.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise UpdateError("暂未找到公开发布版本。") from None
            raise UpdateError(f"更新服务器返回 HTTP {error.code}，请稍后重试。") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise UpdateError("无法连接 GitHub 更新服务，请检查网络后重试。") from None

    def _json(self, url: str, limit: int = 1024 * 1024) -> dict:
        with self._open(url) as response:
            raw = response.read(limit + 1)
        self._check_cancelled()
        if len(raw) > limit:
            raise UpdateError("更新清单超过大小限制。")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise UpdateError("更新服务器返回的清单格式无效。") from None
        if not isinstance(value, dict):
            raise UpdateError("更新清单格式无效。")
        return value

    def check(self, current_version: str) -> Release | None:
        self._cancel.clear()
        current = version_tuple(current_version)
        repository = self._repository()
        self.log("正在检查 GitHub 正式发布版本……")
        latest = self._json(f"https://api.github.com/repos/{repository}/releases/latest")
        tag = latest.get("tag_name", "")
        if latest.get("draft") or latest.get("prerelease") or not isinstance(tag, str) or not tag.startswith("v"):
            raise UpdateError("更新渠道没有有效的稳定版本。")
        target = version_tuple(tag[1:])
        if target <= current:
            return None
        assets = latest.get("assets", [])
        if not isinstance(assets, list):
            raise UpdateError("发布附件列表格式无效。")
        manifest_url = f"https://github.com/{repository}/releases/download/{tag}/update.json"
        manifests = [a for a in assets if isinstance(a, dict) and a.get("name") == "update.json"]
        if len(manifests) != 1 or manifests[0].get("browser_download_url") != manifest_url:
            raise UpdateError("此版本缺少有效的更新清单。")
        envelope = self._json(manifest_url, 96 * 1024)
        manifest = self._signed_manifest(envelope)
        if (manifest.get("schema") != 1 or manifest.get("repository") != repository
                or manifest.get("version") != tag[1:] or manifest.get("tag") != tag):
            raise UpdateError("发布清单的仓库或版本不匹配。")
        if version_tuple(manifest.get("min_updater_version")) > version_tuple(UPDATER_VERSION):
            raise UpdateError("此版本需要新版更新器，请从 GitHub 下载完整便携包。")
        name = f"sms-batch-cleaner-{tag[1:]}-win-x64.zip"
        size, digest = manifest.get("size"), manifest.get("sha256")
        if (manifest.get("asset") != name or type(size) is not int or not 0 < size <= MAX_DOWNLOAD
                or not isinstance(digest, str) or not re.fullmatch("[0-9a-fA-F]{64}", digest)):
            raise UpdateError("发布清单的文件大小或校验值无效。")
        package_url = f"https://github.com/{repository}/releases/download/{tag}/{name}"
        packages = [a for a in assets if isinstance(a, dict) and a.get("name") == name]
        if (len(packages) != 1 or packages[0].get("browser_download_url") != package_url
                or packages[0].get("size") != size):
            raise UpdateError("发布附件与更新清单不匹配。")
        notes = latest.get("body") or ""
        return Release(tag[1:], str(notes)[:20000], manifest_url, package_url,
                       size, digest.lower(), tag, repository, envelope)

    def download(self, release: Release) -> PreparedUpdate:
        self._cancel.clear()
        # Revalidate a passed Release; callers must not turn this into an arbitrary downloader.
        repository = self._repository()
        version_tuple(release.version)
        expected = (f"https://github.com/{repository}/releases/download/v{release.version}/"
                    f"sms-batch-cleaner-{release.version}-win-x64.zip")
        if (release.repository != repository or release.tag != "v" + release.version
                or release.package_url != expected or type(release.size) is not int
                or not 0 < release.size <= MAX_DOWNLOAD
                or not re.fullmatch("[0-9a-f]{64}", release.sha256)):
            raise UpdateError("下载计划无效。")
        manifest = self._signed_manifest(release.signed_manifest or {})
        if (manifest.get("repository") != repository or manifest.get("version") != release.version
                or manifest.get("tag") != release.tag or manifest.get("size") != release.size
                or str(manifest.get("sha256", "")).lower() != release.sha256
                or manifest.get("asset") != expected.rsplit("/", 1)[-1]):
            raise UpdateError("下载计划与签名清单不一致。")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        stage = self.work_dir / ("update-" + uuid.uuid4().hex)
        stage.mkdir()
        archive = stage / "package.zip"
        try:
            downloaded = 0
            started = time.monotonic()
            last_update = 0.0
            with self._open(release.package_url) as response, archive.open("xb") as output:
                while True:
                    self._check_cancelled()
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > release.size:
                        raise UpdateError("下载文件超过发布清单声明的大小。")
                    output.write(chunk)
                    now = time.monotonic()
                    if now - last_update >= 0.1 or downloaded == release.size:
                        self.progress({"phase": "download", "done": downloaded, "total": release.size,
                                       "speed": downloaded / max(now - started, 0.01)})
                        last_update = now
                output.flush()
                os.fsync(output.fileno())
            if downloaded != release.size:
                raise UpdateError("下载不完整，请重新下载。")
            self._verify(archive, release.sha256)
            self._extract(archive, stage)
            package = stage / "SmsCleaner"
            validate_package(package, release.version)
            (stage / "prepared.json").write_text(json.dumps({"schema": 1, "version": release.version,
                "repository": repository, "sha256": release.sha256}, ensure_ascii=False), encoding="utf-8")
            (stage / "signed-update.json").write_text(json.dumps(release.signed_manifest), encoding="utf-8")
            self.log(f"版本 {release.version} 已下载并校验完成。")
            return PreparedUpdate(stage, package, release.version)
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    def _verify(self, archive: Path, expected: str) -> None:
        digest = hashlib.sha256()
        size, done = archive.stat().st_size, 0
        with archive.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                self._check_cancelled()
                digest.update(chunk)
                done += len(chunk)
                self.progress({"phase": "verify", "done": done, "total": size, "speed": 0})
        if digest.hexdigest() != expected:
            raise UpdateError("SHA-256 校验失败，更新包已拒绝安装。")

    def _extract(self, archive: Path, stage: Path) -> None:
        try:
            with zipfile.ZipFile(archive) as zipped:
                entries = zipped.infolist()
                if len(entries) > MAX_FILES or sum(i.file_size for i in entries) > MAX_EXTRACTED:
                    raise UpdateError("更新包解压后的大小或文件数量超过限制。")
                names: set[str] = set()
                spellings: dict[str, str] = {}
                for entry in entries:
                    path = entry.filename
                    parts = PurePosixPath(path).parts
                    if (not parts or parts[0] != "SmsCleaner" or "\\" in path or ":" in path
                            or path.startswith("/") or any(p in (".", "..") for p in path.split("/"))
                            or any(p.endswith((".", " ")) for p in parts)
                            or any(ord(character) < 32 for character in path)
                            or getattr(entry, "orig_filename", path) != path
                            or any(re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", p) for p in parts)
                            or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1):
                        raise UpdateError("更新包包含不安全的文件路径。")
                    if len(parts) == 1:
                        if not entry.is_dir():
                            raise UpdateError("更新包目录结构无效。")
                    elif parts[1] not in ("app", "runtime") and not (len(parts) == 2 and parts[1] in ROOT_FILES):
                        raise UpdateError("更新包试图写入程序范围以外的目录。")
                    key = path.rstrip("/").casefold()
                    if key in names:
                        raise UpdateError("更新包包含重名文件。")
                    names.add(key)
                    for length in range(1, len(parts) + 1):
                        spelling = "/".join(parts[:length])
                        folded = spelling.casefold()
                        if folded in spellings and spellings[folded] != spelling:
                            raise UpdateError("更新包包含大小写冲突的路径。")
                        spellings[folded] = spelling
                total = sum(i.file_size for i in entries)
                done = 0
                for entry in entries:
                    self._check_cancelled()
                    destination = stage.joinpath(*PurePosixPath(entry.filename).parts)
                    if entry.is_dir():
                        destination.mkdir(parents=True, exist_ok=True)
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with zipped.open(entry) as source, destination.open("xb") as output:
                        while chunk := source.read(256 * 1024):
                            self._check_cancelled()
                            output.write(chunk)
                            done += len(chunk)
                            if done > MAX_EXTRACTED:
                                raise UpdateError("解压大小超过限制。")
                            self.progress({"phase": "extract", "done": done, "total": total, "speed": 0})
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as error:
            if isinstance(error, UpdateError):
                raise
            raise UpdateError("更新压缩包损坏或格式不受支持。") from error

    def launch_install(self, prepared: PreparedUpdate, parent_pid: int) -> None:
        self._check_cancelled()
        self.verify_prepared(prepared)
        if prepared.package_dir.resolve() != prepared.stage_dir.resolve() / "SmsCleaner":
            raise UpdateError("更新暂存目录无效。")
        ready_file = prepared.stage_dir / ("worker-ready-" + uuid.uuid4().hex + ".json")
        # Import lazily to avoid bringing GUI/installer code into ordinary update checks.
        from update_worker import process_identity
        parent_start = process_identity(parent_pid)
        if parent_start is None:
            raise UpdateError("无法确认当前程序进程，更新尚未开始。")
        command = [str(prepared.package_dir / "runtime/pythonw.exe"), "-B",
                   str(prepared.package_dir / "app/update_worker.py"),
                   "--install-dir", str(self.install_dir), "--package-dir", str(prepared.package_dir),
                   "--parent-pid", str(parent_pid), "--parent-start", str(parent_start),
                   "--ready-file", str(ready_file), "--channel-dir", str(self.resource_dir),
                   "--version", prepared.version]
        try:
            process = subprocess.Popen(command, cwd=str(prepared.stage_dir), close_fds=True,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            raise UpdateError("无法启动更新安装程序，当前程序未被替换。") from None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if ready_file.exists():
                try:
                    result = json.loads(ready_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    result = {}
                if not isinstance(result, dict):
                    result = {}
                if result.get("ready") is True:
                    if type(result.get("pid")) is not int or result["pid"] != process.pid:
                        raise UpdateError("更新安装程序的启动确认身份不匹配，当前程序继续运行。")
                    if process.poll() is not None:
                        raise UpdateError("更新安装程序提前退出，当前程序未被替换。")
                    return
                if result.get("error"):
                    raise UpdateError("安装程序准备失败：" + str(result["error"])[:500])
            if process.poll() is not None:
                raise UpdateError("更新安装程序提前退出，当前程序未被替换。")
            time.sleep(0.1)
        # Never terminate a worker that may be handling files. It has its own
        # bounded parent wait and will leave the active installation unchanged.
        raise UpdateError("更新窗口未准备就绪，当前程序继续运行；请查看更新日志。")

    def verify_prepared(self, prepared: PreparedUpdate) -> None:
        """Reverify the signed archive AND its staged files before executing them."""
        stage = prepared.stage_dir.resolve()
        package = prepared.package_dir.resolve()
        if package != stage / "SmsCleaner" or not package.is_dir():
            raise UpdateError("更新暂存目录无效。")
        try:
            envelope = json.loads((stage / "signed-update.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise UpdateError("暂存更新缺少签名清单，请重新下载。") from None
        manifest = self._signed_manifest(envelope)
        repository = self._repository()
        version_tuple(prepared.version)
        if (manifest.get("schema") != 1 or manifest.get("repository") != repository
                or manifest.get("version") != prepared.version or manifest.get("tag") != "v" + prepared.version
                or manifest.get("asset") != f"sms-batch-cleaner-{prepared.version}-win-x64.zip"
                or type(manifest.get("size")) is not int or not 0 < manifest["size"] <= MAX_DOWNLOAD
                or not isinstance(manifest.get("sha256"), str)
                or not re.fullmatch("[0-9a-fA-F]{64}", manifest["sha256"])):
            raise UpdateError("暂存更新签名信息不匹配。")
        if version_tuple(manifest.get("min_updater_version")) > version_tuple(UPDATER_VERSION):
            raise UpdateError("当前更新器不支持此更新包。")
        archive = stage / "package.zip"
        if not archive.is_file() or archive.stat().st_size != manifest["size"]:
            raise UpdateError("暂存更新包大小发生变化，请重新下载。")
        self._verify(archive, manifest["sha256"].lower())
        expected_files = set()
        with zipfile.ZipFile(archive) as zipped:
            for entry in zipped.infolist():
                self._check_cancelled()
                if entry.is_dir():
                    continue
                parts = PurePosixPath(entry.filename).parts
                if not parts or parts[0] != "SmsCleaner" or any(part == ".." for part in parts):
                    raise UpdateError("暂存更新包路径无效。")
                relative = Path(*parts[1:])
                expected_files.add(relative.as_posix())
                actual = package / relative
                if actual.is_symlink() or not actual.is_file() or actual.stat().st_size != entry.file_size:
                    raise UpdateError("暂存程序文件发生变化，请重新下载。")
                expected_hash = hashlib.sha256()
                actual_hash = hashlib.sha256()
                with zipped.open(entry) as original, actual.open("rb") as current:
                    while chunk := original.read(1024 * 1024):
                        self._check_cancelled()
                        expected_hash.update(chunk)
                    while chunk := current.read(1024 * 1024):
                        self._check_cancelled()
                        actual_hash.update(chunk)
                if expected_hash.digest() != actual_hash.digest():
                    raise UpdateError("暂存程序文件校验失败，请重新下载。")
        actual_files = set()
        for file in package.rglob("*"):
            if file.is_symlink():
                raise UpdateError("暂存程序目录包含链接，请重新下载。")
            if file.is_file():
                actual_files.add(file.relative_to(package).as_posix())
        if actual_files != expected_files:
            raise UpdateError("暂存程序目录包含未签名文件，请重新下载。")
        validate_package(package, prepared.version)
