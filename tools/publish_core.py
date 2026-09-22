#!/usr/bin/env python3
"""Publish verified immutable core assets, then advance the signed stable feed."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error

from build_core import json_url, request

ROOT = Path(__file__).resolve().parents[1]
REPO = "thetapilla/tailscale-koolshare"
ARCHES = ("arm", "arm64")
MAX_MANIFEST = 65536
STABLE_URL = f"https://github.com/{REPO}/releases/download/core-stable/manifest.json"
STABLE_TITLE = "核心更新索引"
STABLE_NOTES = ("供插件页面“检查更新”读取的签名索引。\n\n"
                f"插件安装包请从[最新发行版](https://github.com/{REPO}/releases/latest)下载。\n")


def core_release_notes(desc):
    version, build = desc["version"], desc["build"]
    return (f"## 更新内容\n\n"
            f"- 基于 Tailscale {version} 构建 ARM32 和 ARM64 精简合并核心，构建编号为 `{build}`。\n"
            "- 上游变更见 [Tailscale 更新日志](https://tailscale.com/changelog)。\n\n"
            "## 使用方式\n\n"
            "在插件页面点击“检查更新”，再点击“更新核心”。\n\n"
            f"插件安装包请从[最新发行版](https://github.com/{REPO}/releases/latest)下载。\n\n"
            "## 来源与校验\n\n"
            f"[官方源码提交](https://github.com/tailscale/tailscale/commit/{desc['source_commit']})；"
            "签名清单、SHA-256 校验值和构建信息见附件。\n")


def gh(*args):
    subprocess.run(["gh", *args, "--repo", REPO], check=True)


def verified(path, arch="arm", helper=None):
    return json.loads(subprocess.check_output([str(helper or ROOT / "build/tsks-helper"), "verify", str(path),
                                              str(ROOT / "plugin/release.pub"), arch]))


def identity(descriptor):
    return (*map(int, descriptor["version"].split(".")), int(descriptor["build"][1:]))


def verify_manifest(data, helper=None):
    if len(data) > MAX_MANIFEST:
        raise ValueError("manifest exceeds maximum size")
    with tempfile.NamedTemporaryFile() as manifest:
        manifest.write(data)
        manifest.flush()
        return {arch: verified(manifest.name, arch, helper) for arch in ARCHES}


def remote_manifest(url, missing_ok=False, helper=None):
    try:
        with request(url) as response:
            data = response.read(MAX_MANIFEST + 1)
    except urllib.error.HTTPError as error:
        if not missing_ok or error.code != 404:
            raise
        error.close()
        return None, None
    return data, verify_manifest(data, helper)


def release_by_tag(tag):
    try:
        return json_url(f"https://api.github.com/repos/{REPO}/releases/tags/{tag}")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        error.close()
        return None


def same_artifacts(actual, expected):
    # The verifier's descriptors contain release identity, provenance, URLs,
    # binary hashes and archive hashes; envelope timestamps are intentionally absent.
    if actual != expected:
        raise ValueError("existing signed release differs from the local candidate")


def archive_matches(path, desc):
    return path.stat().st_size == desc["size"] and hashlib.sha256(path.read_bytes()).hexdigest() == desc["sha256"]


def validate_archives(out):
    descriptors = {}
    for arch in ARCHES:
        desc = verified(out / "manifest.json", arch)
        archive = out / desc["url"].rsplit("/", 1)[1]
        if not archive_matches(archive, desc):
            raise ValueError("archive size/hash differs from signed manifest: " + arch)
        with tempfile.TemporaryDirectory() as temporary:
            descriptor = Path(temporary) / "descriptor.json"
            descriptor.write_text(json.dumps(desc))
            subprocess.run([str(ROOT / "build/tsks-helper"), "extract", str(archive), str(descriptor),
                            str(Path(temporary) / "extracted")], check=True)
        descriptors[arch] = desc
    return descriptors


def readback_assets(descriptors):
    for arch, desc in descriptors.items():
        for attempt in range(3):
            try:
                with request(desc["url"]) as response:
                    digest, size = hashlib.sha256(), 0
                    while chunk := response.read(1024 * 1024):
                        size += len(chunk)
                        if size > desc["size"]:
                            raise ValueError("published archive exceeds signed size: " + arch)
                        digest.update(chunk)
                if size != desc["size"] or digest.hexdigest() != desc["sha256"]:
                    raise ValueError("published archive differs from signed manifest: " + arch)
                break
            except urllib.error.HTTPError as error:
                if attempt == 2 or error.code not in (404, 500, 502, 503, 504):
                    raise
                error.close()
                time.sleep(2)


def existing_manifest(release, tag, descriptors):
    if not release.get("draft"):
        data, remote = remote_manifest(f"https://github.com/{REPO}/releases/download/{tag}/manifest.json")
        same_artifacts(remote, descriptors)
        readback_assets(remote)
        return data
    names = [desc["url"].rsplit("/", 1)[1] for desc in descriptors.values()]
    required = {"manifest.json", "SHA256SUMS", "build-metadata.json", *names}
    uploaded = {asset["name"] for asset in release.get("assets", []) if asset.get("state") == "uploaded"}
    if not required <= uploaded:
        raise RuntimeError("existing draft release is incomplete; manually remove the incomplete draft before retrying")
    # Draft asset URLs are not public. gh authenticates the read-only downloads.
    with tempfile.TemporaryDirectory() as temporary:
        command = ["release", "download", tag, "--dir", temporary]
        for name in ["manifest.json", *names]:
            command.extend(("--pattern", name))
        gh(*command)
        data = (Path(temporary) / "manifest.json").read_bytes()
        remote = verify_manifest(data)
        same_artifacts(remote, descriptors)
        for arch, desc in remote.items():
            if not archive_matches(Path(temporary) / desc["url"].rsplit("/", 1)[1], desc):
                raise ValueError("draft archive differs from signed manifest: " + arch)
    gh("release", "edit", tag, "--draft=false", "--latest=false")
    readback_assets(remote)
    return data


def main():
    out = ROOT / "build/core-release"
    descriptors = validate_archives(out)
    desc = descriptors["arm"]
    tag = f'core-v{desc["version"]}-{desc["build"]}'
    release = release_by_tag(tag)
    stable = release_by_tag("core-stable")
    current = None
    if stable is not None:
        if stable.get("draft"):
            raise RuntimeError("stable feed release is a draft; manually resolve it before retrying")
        _, current = remote_manifest(STABLE_URL, missing_ok=True)
        if current is not None:
            if identity(desc) < identity(current["arm"]):
                raise RuntimeError("refusing to downgrade the signed stable version/build")
            if identity(desc) == identity(current["arm"]):
                same_artifacts(current, descriptors)
    if release is not None:
        envelope = existing_manifest(release, tag, descriptors)
    else:
        assets = [str(out / f'tailscale-core_{desc["version"]}_{desc["build"]}_{arch}.tar.gz') for arch in ARCHES]
        assets += [str(out / name) for name in ("manifest.json", "SHA256SUMS", "build-metadata.json")]
        notes = out / "release-notes.md"
        notes.write_text(core_release_notes(desc))
        gh("release", "create", tag, *assets, "--draft", "--target", os.environ["GITHUB_SHA"],
           "--title", f'Tailscale 核心 {desc["version"]} ({desc["build"]})', "--notes-file", str(notes), "--latest=false")
        gh("release", "edit", tag, "--draft=false", "--latest=false")
        readback_assets(descriptors)
        envelope = (out / "manifest.json").read_bytes()
    if current == descriptors:
        return
    if stable is None:
        gh("release", "create", "core-stable", "--target", os.environ["GITHUB_SHA"], "--title", STABLE_TITLE,
           "--notes", STABLE_NOTES, "--latest=false")
    with tempfile.TemporaryDirectory() as temporary:
        manifest = Path(temporary) / "manifest.json"
        manifest.write_bytes(envelope)
        gh("release", "upload", "core-stable", str(manifest), "--clobber")


if __name__ == "__main__":
    main()
