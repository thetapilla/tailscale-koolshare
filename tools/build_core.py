#!/usr/bin/env python3
"""Build verified upstream source with pinned tools in a confined Docker mount."""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
RECIPE_PATH = ROOT / "tools/core_recipe.json"
RECIPE = json.loads(RECIPE_PATH.read_text())


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def request(url):
    headers = {"User-Agent": "tailscale-koolshare-build/3.0.0"}
    if os.environ.get("GH_TOKEN") and url.startswith("https://api.github.com/"):
        headers["Authorization"] = "Bearer " + os.environ["GH_TOKEN"]
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120)


def json_url(url):
    with request(url) as response:
        return json.load(response)


def source_commit(version):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or int(version.split(".")[1]) % 2:
        raise ValueError("only stable numeric upstream versions are accepted")
    ref = json_url(f"https://api.github.com/repos/tailscale/tailscale/git/ref/tags/v{version}")
    obj = ref["object"]
    for _ in range(4):
        if obj["type"] == "commit":
            if not re.fullmatch(r"[0-9a-f]{40}", obj["sha"]):
                raise ValueError("invalid upstream commit")
            return obj["sha"]
        if obj["type"] != "tag":
            break
        obj = json_url("https://api.github.com/repos/tailscale/tailscale/git/tags/" + obj["sha"])["object"]
    raise ValueError("upstream tag did not resolve to a commit")


def download(url, dest, expected=None):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists() or (expected and sha256(dest) != expected):
        partial = dest.with_suffix(dest.suffix + ".partial")
        with request(url) as response, partial.open("wb") as out:
            while chunk := response.read(1024 * 1024):
                out.write(chunk)
        if expected and sha256(partial) != expected:
            partial.unlink()
            raise ValueError("checksum mismatch: " + str(dest))
        partial.replace(dest)
    return sha256(dest)


def extract(archive, dest):
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tar:
        tar.extractall(dest, filter="data")


def recipe_hash():
    digest = hashlib.sha256()
    for name in ("core_recipe.json", "build_core.py", "container_build.py", "resolve_core.py", "toolchain.py"):
        digest.update(name.encode() + b"\0" + (ROOT / "tools" / name).read_bytes() + b"\0")
    return digest.hexdigest()


def main():
    from resolve_core import lock_hash, resolve, source_requirements, validate_lock
    from toolchain import provision_go

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version")
    parser.add_argument("--lock", type=Path, help="resolved source/toolchain lock carried from discovery")
    parser.add_argument("--arch", choices=("arm", "arm64", "all"), default="all")
    parser.add_argument("--jobs", type=int, default=6, help="Go compiler parallelism per architecture")
    args = parser.parse_args()
    host = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "AMD64": "amd64"}[platform.machine()]
    cache, build = ROOT / ".cache", ROOT / "build"
    build.mkdir(exist_ok=True)
    if args.lock:
        lock = validate_lock(json.loads(args.lock.read_text()), check_recipe=True)
        if args.version and args.version != lock["version"]:
            raise ValueError("requested version differs from dependency lock")
    else:
        version = args.version or RECIPE["initial_version"]
        commit = source_commit(version)
        path = cache / "locks" / f'core-{version}-{RECIPE["build"]}-{recipe_hash()}.json'
        lock = resolve(version, commit, path, reuse_published=False)
    version, commit = lock["version"], lock["source"]["commit"]
    if lock["build"] != RECIPE["build"]:
        raise ValueError("locked build differs from current recipe")
    source_name = "tailscale-" + commit
    source_archive = cache / "downloads" / (source_name + ".tar.gz")
    download(lock["source"]["url"], source_archive, lock["source"]["sha256"])
    go_mod, preferred, revision = source_requirements(source_archive, version, commit)
    from toolchain import _requirements
    if _requirements(go_mod, preferred) != lock["go"]["requirements"] or revision != lock["upstream_toolchain_rev"]:
        raise ValueError("locked Go requirements differ from the source archive")
    source = cache / "source" / source_name
    if source.exists():
        shutil.rmtree(source)
    extract(source_archive, cache / "source")
    go_dir = provision_go(lock["go"], host, cache, "core", download, extract)
    upx_version = RECIPE["upx_version"]
    filename = f"upx-{upx_version}-{host}_linux.tar.xz"
    archive = cache / "downloads" / filename
    download(f"https://github.com/upx/upx/releases/download/v{upx_version}/{filename}", archive, RECIPE["upx_sha256"][host])
    upx_dir = cache / "toolchains/upx" / upx_version / (host + "-" + RECIPE["upx_sha256"][host][:16])
    if upx_dir.exists():
        shutil.rmtree(upx_dir)
    extract(archive, upx_dir)
    metadata = {"schema": 2, "version": version, "build": lock["build"], "source_commit": commit,
                "source_sha256": lock["source"]["sha256"], "recipe_sha256": lock_hash(lock),
                "recipe_source_sha256": lock["recipe_source_sha256"], "go_version": lock["go"]["version"],
                "upx_version": upx_version, "tags": RECIPE["tags"], "host_arch": host,
                "source_date_epoch": 0, "dependency_lock": lock}
    (build / "core-lock.json").write_text(json.dumps(lock, sort_keys=True, indent=2) + "\n")
    (build / "core-build.json").write_text(json.dumps(metadata, sort_keys=True, indent=2) + "\n")
    arches = ("arm", "arm64") if args.arch == "all" else (args.arch,)
    go_executable = "/cache/" + (go_dir / "go/bin/go").relative_to(cache).as_posix()
    upx_executable = "/cache/" + (upx_dir / filename.removesuffix(".tar.xz") / "upx").relative_to(cache).as_posix()

    def build_one(arch):
        command = ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                   "--mount", f"type=bind,src={ROOT / 'tools'},dst=/repo/tools,readonly",
                   "--mount", f"type=bind,src={cache},dst=/cache",
                   "--mount", f"type=bind,src={build},dst=/out",
                   RECIPE["container"], "python3", "/repo/tools/container_build.py",
                   arch, host, source_name, str(args.jobs), go_executable, upx_executable]
        subprocess.run(command, check=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(arches)) as pool:
        list(pool.map(build_one, arches))


if __name__ == "__main__":
    main()
