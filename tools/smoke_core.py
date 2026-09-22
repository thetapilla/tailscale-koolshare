#!/usr/bin/env python3
"""Exercise both actual UPX cores without joining a tailnet or using the network."""
import argparse
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "debian:bookworm-slim@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", choices=("all", "arm", "arm64"), default="all")
    args = parser.parse_args()
    version = json.loads((ROOT / "build/core-build.json").read_text())["version"]
    arches = ("arm", "arm64") if args.arch == "all" else (args.arch,)
    for arch in arches:
        platform = "linux/arm/v7" if arch == "arm" else "linux/arm64"
        subprocess.run(["docker", "run", "--rm", "--platform", platform, "--network=none", "--read-only",
                        "--tmpfs", "/tmp:rw,exec,nosuid,size=128m", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                        "--mount", f"type=bind,src={ROOT / 'build/cores' / arch},dst=/input,readonly",
                        "--mount", f"type=bind,src={ROOT / 'build/helpers' / arch},dst=/helper,readonly",
                        "--mount", f"type=bind,src={ROOT / 'tools/smoke_core.sh'},dst=/smoke.sh,readonly",
                        IMAGE, "sh", "/smoke.sh", version], check=True, timeout=90)


if __name__ == "__main__":
    main()
