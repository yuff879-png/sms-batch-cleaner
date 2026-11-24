"""Publish verified release assets using the operator's Git credential manager.

This script never stores credentials or bundles them into the application.
Run only after committing/tagging the intended release and building its assets.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from version import VERSION

ROOT = Path(__file__).resolve().parent


def github_token():
    result = subprocess.run(["git", "credential", "fill"],
                            input="protocol=https\nhost=github.com\n\n", text=True,
                            capture_output=True, timeout=30,
                            env=dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never"))
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if result.returncode or not fields.get("password"):
        raise RuntimeError("GitHub authentication is unavailable in Git Credential Manager")
    return fields["password"]


def api(path, *, token, method="GET", body=None, binary=None):
    url = path if path.startswith("https://") else "https://api.github.com/" + path
    if urllib.parse.urlparse(url).hostname not in ("api.github.com", "uploads.github.com"):
        raise ValueError("Unexpected GitHub API host")
    headers = {"Authorization": "Bearer " + token, "User-Agent": "sms-batch-cleaner-publisher",
               "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if binary is not None:
        data = binary
        headers["Content-Type"] = "application/octet-stream"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.load(response)


def verified_assets():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    channel = json.loads((ROOT / "update-channel.json").read_text(encoding="utf-8"))
    manifest_path = ROOT / "dist" / "update.json"
    envelope = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload = base64.b64decode(envelope["payload"], validate=True)
    public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(channel["public_key"], validate=True))
    public_key.verify(base64.b64decode(envelope["signature"], validate=True), payload)
    release = json.loads(payload)
    if release["version"] != VERSION or release["repository"] != channel["repository"]:
        raise ValueError("Release version/repository mismatch")
    archive = ROOT / "dist" / release["asset"]
    if archive.parent != ROOT / "dist" or not archive.is_file():
        raise ValueError("Invalid release archive")
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != release["sha256"] or archive.stat().st_size != release["size"]:
        raise ValueError("Release archive checksum mismatch")
    return channel["repository"], [archive, manifest_path, ROOT / "dist" / "SHA256SUMS.txt"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--publish", action="store_true", help="Create a draft, upload verified assets, then publish")
    args = parser.parse_args()
    repository, assets = verified_assets()
    print(f"Verified release v{VERSION}: {repository}, {len(assets)} assets")
    if not args.publish:
        return
    status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                            cwd=ROOT, capture_output=True, text=True, check=True).stdout
    if status.strip():
        raise RuntimeError("Commit tracked source changes before publishing")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                            text=True, check=True).stdout.strip()
    tag_commit = subprocess.run(["git", "rev-list", "-n", "1", "v" + VERSION], cwd=ROOT,
                                capture_output=True, text=True, check=True).stdout.strip()
    if tag_commit != commit:
        raise RuntimeError("Release tag must point to current HEAD")
    token = github_token()
    remote_tag = api(f"repos/{repository}/git/ref/tags/v{VERSION}", token=token)
    if remote_tag["object"]["sha"] != commit or remote_tag["object"]["type"] != "commit":
        raise RuntimeError("Push a lightweight release tag matching HEAD before publishing")
    release = api(f"repos/{repository}/releases", token=token, method="POST", body={
        "tag_name": "v" + VERSION, "name": "短信批量清理工具 v" + VERSION,
        "body": (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"),
        "draft": True, "prerelease": False, "target_commitish": commit,
    })
    upload_url = release["upload_url"].split("{", 1)[0]
    for asset in assets:
        uploaded = api(upload_url + "?name=" + urllib.parse.quote(asset.name), token=token,
                       method="POST", binary=asset.read_bytes())
        if uploaded.get("size") != asset.stat().st_size or uploaded.get("state") != "uploaded":
            raise RuntimeError("Asset upload not confirmed; release remains a draft")
        print("Uploaded", asset.name, uploaded["size"])
    published = api(f"repos/{repository}/releases/{release['id']}", token=token,
                    method="PATCH", body={"draft": False, "make_latest": "true"})
    print("Published", published["html_url"])


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as error:
        raise SystemExit(f"GitHub API failed: HTTP {error.code}; credentials were not logged") from None
