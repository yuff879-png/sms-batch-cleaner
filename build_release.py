"""Build a portable Windows package using the official signed Python runtime."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from version import VERSION

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform-tools", type=Path, default=Path(r"C:\platform-tools"))
    parser.add_argument("--python-runtime", type=Path, default=Path(sys.base_prefix))
    parser.add_argument("--signing-key", type=Path,
                        default=Path.home() / ".codex" / "release-keys" / "sms-batch-cleaner-ed25519.pem")
    args = parser.parse_args()
    bridge = ROOT / "android" / "build" / "bridge.jar"
    if not bridge.is_file():
        raise SystemExit("First compile android/build.ps1")
    build = ROOT / "build"
    dist = ROOT / "dist"
    build.mkdir(exist_ok=True)
    dist.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="portable-stage-", dir=build) as temporary:
        stage = Path(temporary).resolve()
        if not stage.is_relative_to(build.resolve()):
            raise SystemExit("Staging path must be inside the project build directory")
        package = stage / "SmsCleaner"
        application = package / "app"
        runtime = package / "runtime"
        platform_tools = application / "platform-tools"
        platform_tools.mkdir(parents=True)
        runtime.mkdir()
        for name in ("app.py", "core.py", "version.py", "updater.py", "update_worker.py", "update-channel.json"):
            shutil.copy2(ROOT / name, application / name)
        shutil.copy2(bridge, application / "bridge.jar")
        for name in ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll", "NOTICE.txt"):
            shutil.copy2(args.platform_tools / name, platform_tools / name)
        optional = args.platform_tools / "libwinpthread-1.dll"
        if optional.exists():
            shutil.copy2(optional, platform_tools / optional.name)
        for name in ("python.exe", "pythonw.exe", "python3.dll", "LICENSE.txt"):
            shutil.copy2(args.python_runtime / name, runtime / name)
        version = str(sys.version_info.major) + str(sys.version_info.minor)
        for file in list(args.python_runtime.glob("python" + version + "*.dll")) + list(args.python_runtime.glob("vcruntime*.dll")):
            shutil.copy2(file, runtime / file.name)
        ignore = shutil.ignore_patterns("__pycache__", "site-packages", "test", "tests", "idlelib", "ensurepip", "turtledemo", "demos")
        for name in ("Lib", "DLLs", "tcl"):
            shutil.copytree(args.python_runtime / name, runtime / name, ignore=ignore)
        import cryptography
        shutil.copytree(Path(cryptography.__file__).parent, runtime / "Lib" / "cryptography", ignore=ignore)
        import cffi
        import _cffi_backend
        shutil.copytree(Path(cffi.__file__).parent, runtime / "Lib" / "cffi", ignore=ignore)
        shutil.copy2(Path(_cffi_backend.__file__), runtime / "DLLs" / Path(_cffi_backend.__file__).name)
        import importlib.metadata
        for dependency in ("cryptography", "cffi"):
            distribution = importlib.metadata.distribution(dependency)
            license_dir = runtime / "licenses" / dependency
            license_dir.mkdir(parents=True)
            for entry in distribution.files or []:
                if "license" in str(entry).lower() and distribution.locate_file(entry).is_file():
                    shutil.copy2(distribution.locate_file(entry), license_dir / Path(str(entry)).name)
        (runtime / ("python" + version + "._pth")).write_text("DLLs\nLib\n..\\app\n", encoding="ascii")
        launcher = "@echo off\r\nsetlocal\r\ncd /d \"%~dp0\"\r\nstart \"\" \"%~dp0runtime\\pythonw.exe\" \"%~dp0app\\app.py\"\r\n"
        # This launcher only starts the signed, bundled Python executable.
        (package / "启动短信清理工具.cmd").write_text(launcher, encoding="utf-8", newline="")
        (package / "portable.flag").write_text("sms-cleaner-v1", encoding="ascii")
        for name in ("使用说明.txt", "README.md", "CHANGELOG.md"):
            shutil.copy2(ROOT / name, package / name)
        release = dist / ("SmsCleaner-" + VERSION + "-Portable")
        shutil.copytree(package, release, dirs_exist_ok=True)
        archive = dist / ("sms-batch-cleaner-" + VERSION + "-win-x64.zip")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
            for path in package.rglob("*"):
                if path.is_file():
                    output.write(path, Path("SmsCleaner") / path.relative_to(package))
        with archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        archive.with_suffix(".zip.sha256").write_text(digest + "\n", encoding="ascii")
        channel = json.loads((ROOT / "update-channel.json").read_text(encoding="utf-8"))
        payload = {"schema": 1, "version": VERSION, "tag": "v" + VERSION,
                   "min_updater_version": "1.1.0", "repository": channel["repository"],
                   "asset": archive.name, "size": archive.stat().st_size, "sha256": digest,
                   "notes": (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"),
                   "published_at": datetime.now(timezone.utc).isoformat()}
        from cryptography.hazmat.primitives import serialization
        signing_key = serialization.load_pem_private_key(args.signing_key.read_bytes(), password=None)
        public_key = base64.b64encode(signing_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode("ascii")
        if public_key != channel["public_key"]:
            raise SystemExit("Signing key does not match the embedded update channel public key")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        manifest = {"payload": base64.b64encode(encoded).decode("ascii"),
                    "signature": base64.b64encode(signing_key.sign(encoded)).decode("ascii")}
        (dist / "update.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        (dist / "SHA256SUMS.txt").write_text(digest + "  " + archive.name + "\n", encoding="ascii")
        print(f"Release: {archive}\nSHA256: {digest}\nBytes: {archive.stat().st_size}")


if __name__ == "__main__":
    main()
