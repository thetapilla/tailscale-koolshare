#!/usr/bin/env python3
"""Build offline state generators against the exact core source and Go lock."""
import json
import fcntl
import hashlib
import os
from pathlib import Path
import platform
import subprocess

from resolve_core import validate_lock, lock_hash
from toolchain import toolchain_dir

ROOT = Path(__file__).resolve().parents[1]


def build():
    lock = validate_lock(json.loads((ROOT / "build/core-lock.json").read_text()), check_recipe=True)
    recipe = json.loads((ROOT / "tools/core_recipe.json").read_text())
    host = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64"}[platform.machine()]
    go = str(toolchain_dir("/cache", "core", lock["go"], host) / "go/bin/go")
    inputs = {"lock": lock_hash(lock), "source": hashlib.sha256(
        (ROOT / "tools/testdata/mkstate/main.go").read_bytes()).hexdigest()}
    for arch in ("arm", "arm64"):
        out = ROOT / "build/core-fixtures" / arch
        out.mkdir(parents=True, exist_ok=True)
        binary, record = out / "mkstate", out / "build.json"
        if record.is_file() and binary.is_file():
            cached = json.loads(record.read_text())
            if all(cached.get(k) == v for k, v in inputs.items()) and cached.get("binary") == hashlib.sha256(binary.read_bytes()).hexdigest():
                print(f"Reused offline state generator: {arch}", flush=True)
                continue
        temporary = "mkstate.new"
        subprocess.run([
            "docker", "run", "--rm", "--network=none", "--user", f"{os.getuid()}:{os.getgid()}",
            "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--mount", f"type=bind,src={ROOT / '.cache'},dst=/cache",
            "--mount", f"type=bind,src={ROOT / 'tools/testdata/mkstate/main.go'},dst=/fixture.go,readonly",
            "--mount", f"type=bind,src={out},dst=/out",
            "--workdir", "/cache/source/tailscale-" + lock["source"]["commit"],
            "-e", "CGO_ENABLED=0", "-e", "GOOS=linux", "-e", "GOARCH=" + arch,
            "-e", "GOARM=7", "-e", "GOARM64=v8.0", "-e", "GOTOOLCHAIN=local",
            "-e", "GOENV=off", "-e", "GOWORK=off", "-e", "GOTELEMETRY=off",
            "-e", "GOPROXY=off", "-e", "GOSUMDB=off", "-e", "GOMODCACHE=/cache/gomod",
            "-e", f"GOCACHE=/cache/gobuild/{lock_hash(lock)}/{arch}",
            recipe["container"], go, "build", "-mod=readonly", "-buildvcs=false", "-trimpath",
            "-tags=" + ",".join(recipe["tags"]), "-ldflags=-s -w -buildid=",
            "-o", "/out/" + temporary, "/fixture.go",
        ], check=True)
        (out / temporary).replace(binary)
        record.write_text(json.dumps({**inputs, "binary": hashlib.sha256(binary.read_bytes()).hexdigest()}, sort_keys=True) + "\n")
        print(f"Built offline state generator: {arch}", flush=True)


def main():
    directory = ROOT / "build/core-fixtures"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".build.lock").open("w") as mutex:
        fcntl.flock(mutex, fcntl.LOCK_EX)
        build()


if __name__ == "__main__":
    main()
