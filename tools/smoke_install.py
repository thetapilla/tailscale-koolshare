#!/usr/bin/env python3
"""Install release archives with real ARM payloads and a restricted command PATH.

Runs without network, host capabilities, TUN or router access. Platform settings,
software-center configuration, free space, firewall and cron are simulated. The
packaged installer, lifecycle library, signatures, helpers and cores are real.
This complements the BusyBox shell fixtures; the container shell is Debian sh.
The optional lifecycle case enables a userspace daemon and exercises LocalAPI,
FIFO logging and public job files without testing packet forwarding.
"""
import argparse
from pathlib import Path
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "debian:bookworm-slim@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818"
PLATFORMS = {"hnd": "arm", "qca": "arm", "ipq32": "arm", "ipq64": "arm64", "mtk": "arm64"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=ROOT.parent / "dist")
    parser.add_argument("--platform", choices=("all", *PLATFORMS), default="all")
    parser.add_argument("--expect-missing-od", action="store_true",
                        help="reproduce the missing-od failure in an older release archive")
    parser.add_argument("--lifecycle", action="store_true",
                        help="also exercise enabled service and public jobs with a userspace daemon")
    args = parser.parse_args()
    if args.lifecycle and args.expect_missing_od:
        parser.error("--lifecycle requires corrected release archives")
    dist = args.dist.resolve(strict=True)
    version = (ROOT / "VERSION").read_text().strip()
    selected = list(PLATFORMS) if args.platform == "all" else [args.platform]
    for name in ["universal", *selected]:
        archive = dist / f"tailscale_{version}_{name}.tar.gz"
        if not archive.is_file():
            parser.error(f"missing release archive: {archive}")
    for arch in ("arm64", "arm"):
        names = [name for name in selected if PLATFORMS[name] == arch]
        if not names:
            continue
        platform = "linux/arm/v7" if arch == "arm" else "linux/arm64"
        name = "tsks-install-smoke-" + uuid.uuid4().hex[:12]
        try:
            subprocess.run([
                "docker", "run", "--rm", "--name", name, "--platform", platform, "--network=none", "--read-only",
                "--tmpfs", "/tmp:rw,exec,nosuid,size=384m", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--mount", f"type=bind,src={dist},dst=/archives,readonly",
                "--mount", f"type=bind,src={ROOT / 'tools/smoke_install.sh'},dst=/smoke.sh,readonly",
                "-e", "TSKS_SMOKE_LIFECYCLE=" + str(int(args.lifecycle)),
                IMAGE, "sh", "/smoke.sh", version, arch,
                "missing-od" if args.expect_missing_od else "install", *names,
            ], check=True, timeout=600)
        finally:
            # A timed-out Docker client does not reliably remove its container.
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=20)


if __name__ == "__main__":
    main()
