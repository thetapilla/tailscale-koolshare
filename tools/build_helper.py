#!/usr/bin/env python3
"""Build the verifier/helper with a toolchain selected from its own source."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile

from build_core import download, extract, json_url
from toolchain import provision_go, resolve_go, toolchain_dir, validate_go_lock

ROOT = Path(__file__).resolve().parents[1]
GO_CATALOG = "https://go.dev/dl/?mode=json&include=all"


def host_arch():
    return {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "AMD64": "amd64"}[platform.machine()]


def helper_recipe(root=ROOT):
    return json.loads((root / "tools/helper_recipe.json").read_text())


def source_fingerprints(root):
    return {
        "go_mod_sha256": hashlib.sha256((root / "go.mod").read_bytes()).hexdigest(),
        "recipe_sha256": hashlib.sha256((root / "tools/helper_recipe.json").read_bytes()).hexdigest(),
    }


def validate_helper_lock(document, root):
    if not isinstance(document, dict) or set(document) != {"schema", "go_mod_sha256", "recipe_sha256", "go"}:
        raise ValueError("invalid helper toolchain lock fields")
    if document["schema"] != 1:
        raise ValueError("unsupported helper toolchain lock schema")
    validate_go_lock(document["go"])
    if any(document[key] != value for key, value in source_fingerprints(root).items()):
        raise ValueError("helper toolchain lock differs from its source or recipe")
    # Re-resolve the source requirements against the locked official release.
    # This also rejects a structurally valid lock copied from the core build.
    locked = document["go"]
    catalog = [{"version": "go" + locked["version"], "stable": True, "files": [
        {**archive, "version": "go" + locked["version"], "os": "linux", "arch": arch, "kind": "archive"}
        for arch, archive in locked["archives"].items()
    ]}]
    selected = resolve_go((root / "go.mod").read_text(), helper_recipe(root)["go_baseline"], catalog)
    if selected != locked:
        raise ValueError("helper toolchain lock does not match its own Go requirements")
    return locked


def prepare_helper_toolchain(root=ROOT, catalog=None):
    """Select and persist the helper's Go lock; no Docker or archive downloads."""
    root = Path(root)
    path = root / "build/helper-toolchain.json"
    fingerprints = source_fingerprints(root)
    if path.is_file():
        document = json.loads(path.read_text())
        if isinstance(document, dict) and all(document.get(key) == value for key, value in fingerprints.items()):
            return validate_helper_lock(document, root)
    lock = resolve_go((root / "go.mod").read_text(), helper_recipe(root)["go_baseline"],
                      json_url(GO_CATALOG) if catalog is None else catalog)
    document = {"schema": 1, **fingerprints, "go": lock}
    validate_helper_lock(document, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".helper-toolchain-", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(document, stream, sort_keys=True, indent=2)
        stream.write("\n")
    temporary.replace(path)
    return lock


def cache_key(lock):
    return "go" + lock["version"] + "-" + hashlib.sha256(
        json.dumps(lock, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def provision_helper_go(root, host, lock):
    return provision_go(lock, host, root / ".cache", "helper", download, extract)


def upx_dir(cache, host, recipe):
    return Path(cache) / "toolchains/helper" / ("upx" + recipe["upx_version"]) / (host + "-" + recipe["upx_sha256"][host][:16])


def provision_helper_upx(root, host, recipe):
    filename = f'upx-{recipe["upx_version"]}-{host}_linux.tar.xz'
    archive = root / ".cache/downloads" / filename
    download(f'https://github.com/upx/upx/releases/download/v{recipe["upx_version"]}/{filename}',
             archive, recipe["upx_sha256"][host])
    target = upx_dir(root / ".cache", host, recipe)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Extract from the verified archive each time, replacing the whole directory.
    with tempfile.TemporaryDirectory(prefix=".upx-extract-", dir=target.parent) as temporary:
        staging = Path(temporary) / "upx"
        extract(archive, staging)
        executable = staging / filename.removesuffix(".tar.xz") / "upx"
        if executable.is_symlink() or not executable.is_file() or not executable.stat().st_mode & 0o111:
            raise ValueError("UPX archive has no executable")
        if target.is_symlink():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)
        staging.rename(target)
    return target


def inside(host, native_os, native_arch):
    root = Path("/repo")
    recipe = helper_recipe(root)
    lock = validate_helper_lock(json.loads(Path("/out/helper-toolchain.json").read_text()), root)
    go = str(toolchain_dir("/cache", "helper", lock, host) / "go/bin/go")
    upx = str(upx_dir("/cache", host, recipe) / f'upx-{recipe["upx_version"]}-{host}_linux/upx')
    for system, arch in (("linux", "arm"), ("linux", "arm64"), (native_os, native_arch)):
        native = (system, arch) == (native_os, native_arch) and system != "linux"
        output = Path("/out/tsks-helper") if native else Path("/out/helpers") / arch / "tsks-helper"
        output.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, GOOS=system, GOARCH=arch, GOARM="7", GOARM64="v8.0", CGO_ENABLED="0",
                   GOTOOLCHAIN="local", GOENV="off", GOTELEMETRY="off",
                   GOCACHE=f"/cache/helper-build/{cache_key(lock)}/{system}-{arch}", GOMODCACHE="/cache/helper-gomod")
        subprocess.run([go, "build", "-mod=readonly", "-buildvcs=false", "-trimpath", "-ldflags=-s -w -buildid=",
                        "-o", str(output), "./cmd/tsks-helper"], cwd="/repo", env=env, check=True)
        if not native:
            subprocess.run([upx, *recipe["upx_args"], str(output)], check=True)
            subprocess.run([upx, "--test", str(output)], check=True)
        output.chmod(0o755)
        print(f"{output}: {output.stat().st_size} bytes", flush=True)
    if native_os == "linux":
        shutil.copyfile(Path("/out/helpers") / native_arch / "tsks-helper", "/out/tsks-helper")
        Path("/out/tsks-helper").chmod(0o755)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-toolchain", action="store_true", help="write the helper Go lock and print its version without building")
    parser.add_argument("--inside", nargs=3, metavar=("HOST", "OS", "ARCH"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inside:
        inside(*args.inside)
        return
    lock = prepare_helper_toolchain()
    if args.prepare_toolchain:
        print(lock["version"])
        return
    host, recipe = host_arch(), helper_recipe()
    provision_helper_go(ROOT, host, lock)
    provision_helper_upx(ROOT, host, recipe)
    subprocess.run(["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "--network=none", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                    "--mount", f"type=bind,src={ROOT / 'tools'},dst=/repo/tools,readonly",
                    "--mount", f"type=bind,src={ROOT / 'cmd'},dst=/repo/cmd,readonly",
                    "--mount", f"type=bind,src={ROOT / 'go.mod'},dst=/repo/go.mod,readonly",
                    "--mount", f"type=bind,src={ROOT / '.cache'},dst=/cache",
                    "--mount", f"type=bind,src={ROOT / 'build'},dst=/out", recipe["container"],
                    "python3", "/repo/tools/build_helper.py", "--inside", host, platform.system().lower(), host], check=True)


if __name__ == "__main__":
    main()
