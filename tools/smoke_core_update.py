#!/usr/bin/env python3
"""Switch signed ARM cores with the real updater, helper and userspace daemon.

Both release directories must contain manifest.json and the archives named in
that signed manifest. The fixture verifies and extracts them inside a disposable
network-isolated container. It exercises a fresh, logged-out identity; it does
not join a tailnet or validate hardware TUN, firewall rules or packet forwarding.
"""
import argparse
from pathlib import Path
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "debian:bookworm-slim@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-release", type=Path, required=True)
    parser.add_argument("--candidate-release", type=Path, default=ROOT / "build/core-release")
    parser.add_argument("--arch", choices=("all", "arm", "arm64"), default="all")
    args = parser.parse_args()
    previous = args.previous_release.resolve(strict=True)
    candidate = args.candidate_release.resolve(strict=True)
    for release in (previous, candidate):
        if not (release / "manifest.json").is_file():
            parser.error(f"missing signed manifest: {release}")
    for arch in (("arm64", "arm") if args.arch == "all" else (args.arch,)):
        helper = ROOT / "build/helpers" / arch
        if not (helper / "tsks-helper").is_file():
            parser.error(f"missing {arch} helper; run tools/build_helper.py first")
        platform = "linux/arm/v7" if arch == "arm" else "linux/arm64"
        name = "tsks-core-update-smoke-" + uuid.uuid4().hex[:12]
        try:
            subprocess.run([
                "docker", "run", "--rm", "--name", name, "--platform", platform,
                "--network=none", "--read-only", "--tmpfs", "/tmp:rw,exec,nosuid,size=192m",
                "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--mount", f"type=bind,src={previous},dst=/previous,readonly",
                "--mount", f"type=bind,src={candidate},dst=/candidate,readonly",
                "--mount", f"type=bind,src={helper},dst=/helper,readonly",
                "--mount", f"type=bind,src={ROOT / 'plugin'},dst=/plugin,readonly",
                "--mount", f"type=bind,src={ROOT / 'tools/smoke_core_update.sh'},dst=/smoke.sh,readonly",
                IMAGE, "sh", "/smoke.sh", arch,
            ], check=True, timeout=600)
        finally:
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=20)


if __name__ == "__main__":
    main()
