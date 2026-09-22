#!/usr/bin/env python3
"""Run current helper source tests on Linux, including real Unix sockets."""
from pathlib import Path
import os
import platform
import subprocess

from build_core import RECIPE

ROOT = Path(__file__).resolve().parents[1]


def main():
    host = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "AMD64": "amd64"}[platform.machine()]
    subprocess.run(["docker", "run", "--rm", "--network=none", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                    "--user", f"{os.getuid()}:{os.getgid()}",
                    "--mount", f"type=bind,src={ROOT / 'cmd'},dst=/repo/cmd,readonly",
                    "--mount", f"type=bind,src={ROOT / 'go.mod'},dst=/repo/go.mod,readonly",
                    "--mount", f"type=bind,src={ROOT / '.cache'},dst=/cache",
                    "-w", "/repo", "-e", "GOCACHE=/cache/helper-test-cache", "-e", "GOTOOLCHAIN=local",
                    RECIPE["container"], f"/cache/toolchains/{host}/go/bin/go", "test", "-v", "./cmd/tsks-helper"], check=True)


if __name__ == "__main__":
    main()
