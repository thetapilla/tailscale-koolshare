#!/usr/bin/env python3
"""Exercise both actual UPX cores without joining a tailnet or using the network."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "debian:bookworm-slim@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", choices=("all", "arm", "arm64"), default="all")
    parser.add_argument("--reference-release", type=Path, help="signed older core release for migration compatibility")
    args = parser.parse_args()
    subprocess.run([sys.executable, str(ROOT / "tools/build_core_fixture.py")], check=True)
    omitted = "ts_omit_clientupdate" in json.loads((ROOT / "tools/core_recipe.json").read_text())["tags"]
    version = json.loads((ROOT / "build/core-build.json").read_text())["version"]
    arches = ("arm", "arm64") if args.arch == "all" else (args.arch,)
    for arch in arches:
        platform = "linux/arm/v7" if arch == "arm" else "linux/arm64"
        reference = [] if args.reference_release is None else ["--mount", f"type=bind,src={args.reference_release.resolve(strict=True)},dst=/reference,readonly"]
        subprocess.run(["docker", "run", "--rm", "--platform", platform, "--network=none", "--read-only",
                        "--tmpfs", "/tmp:rw,exec,nosuid,size=128m", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                        "--mount", f"type=bind,src={ROOT / 'build/cores' / arch},dst=/input,readonly",
                        "--mount", f"type=bind,src={ROOT / 'build/helpers' / arch},dst=/helper,readonly",
                        "--mount", f"type=bind,src={ROOT / 'tools/smoke_core.sh'},dst=/smoke.sh,readonly",
                        "--mount", f"type=bind,src={ROOT / 'build/core-fixtures' / arch},dst=/fixture,readonly",
                        "--mount", f"type=bind,src={ROOT / 'plugin/release.pub'},dst=/release.pub,readonly",
                        *reference,
                        IMAGE, "sh", "/smoke.sh", version, str(int(omitted)), arch], check=True, timeout=240)


if __name__ == "__main__":
    main()
