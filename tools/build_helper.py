#!/usr/bin/env python3
"""Cross-build the stdlib-only verifier/helper using the prepared pinned toolchain."""
import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys


def inside(host, native_os, native_arch):
    recipe = json.loads(Path("/repo/tools/core_recipe.json").read_text())
    tools = Path("/cache/toolchains") / host
    go = str(tools / "go/bin/go")
    upx = str(tools / f'upx-{recipe["upx_version"]}-{host}_linux/upx')
    for system, arch in (("linux", "arm"), ("linux", "arm64"), (native_os, native_arch)):
        native = (system, arch) == (native_os, native_arch) and system != "linux"
        output = Path("/out/tsks-helper") if native else Path("/out/helpers") / arch / "tsks-helper"
        output.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, GOOS=system, GOARCH=arch, GOARM="7", GOARM64="v8.0", CGO_ENABLED="0",
                   GOTOOLCHAIN="local", GOENV="off", GOTELEMETRY="off", GOCACHE="/cache/helper-build-" + arch, GOMODCACHE="/cache/gomod")
        subprocess.run([go, "build", "-mod=readonly", "-buildvcs=false", "-trimpath", "-ldflags=-s -w -buildid=",
                        "-o", str(output), "./cmd/tsks-helper"], cwd="/repo", env=env, check=True)
        if not native:
            subprocess.run([upx, "--best", "--lzma", str(output)], check=True)
            subprocess.run([upx, "--test", str(output)], check=True)
        output.chmod(0o755)
        print(f"{output}: {output.stat().st_size} bytes", flush=True)
    if native_os == "linux":
        import shutil
        shutil.copyfile(Path("/out/helpers") / native_arch / "tsks-helper", "/out/tsks-helper")
        Path("/out/tsks-helper").chmod(0o755)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--inside":
        inside(*sys.argv[2:])
        return
    root = Path(__file__).resolve().parents[1]
    host = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "AMD64": "amd64"}[platform.machine()]
    recipe = json.loads((root / "tools/core_recipe.json").read_text())
    subprocess.run(["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "--network=none", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                    "--mount", f"type=bind,src={root / 'tools'},dst=/repo/tools,readonly",
                    "--mount", f"type=bind,src={root / 'cmd'},dst=/repo/cmd,readonly",
                    "--mount", f"type=bind,src={root / 'go.mod'},dst=/repo/go.mod,readonly",
                    "--mount", f"type=bind,src={root / '.cache'},dst=/cache",
                    "--mount", f"type=bind,src={root / 'build'},dst=/out", recipe["container"],
                    "python3", "/repo/tools/build_helper.py", "--inside", host, platform.system().lower(), host], check=True)


if __name__ == "__main__":
    main()
