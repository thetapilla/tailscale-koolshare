#!/usr/bin/env python3
"""Discover stable upstream versions; skip only a verified current/newer feed."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from build_core import RECIPE, json_url, source_commit
from publish_core import STABLE_URL, identity, remote_manifest

ROOT = Path(__file__).resolve().parents[1]


def update_needed(version, helper):
    # A fresh checkout without a native verifier must never trust an unsigned
    # payload or the mere existence of an immutable release to suppress repair.
    if not helper.is_file():
        return True
    try:
        _, current = remote_manifest(STABLE_URL, missing_ok=True, helper=helper)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Signed stable feed could not be verified; requesting a build: {type(error).__name__}", file=sys.stderr)
        return True
    return current is None or identity(current["arm"]) < identity({"version": version, "build": RECIPE["build"]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="")
    parser.add_argument("--helper", type=Path, default=ROOT / "build/tsks-helper")
    args = parser.parse_args()
    if args.version:
        version = args.version.removeprefix("v")
    else:
        release = json_url("https://pkgs.tailscale.com/stable/?mode=json")
        version = release["TarballsVersion"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or int(version.split(".")[1]) % 2:
        raise ValueError("expected stable numeric Tailscale version")
    source_commit(version)  # A matching official stable GitHub source tag must exist.
    tag = f'core-v{version}-{RECIPE["build"]}'
    needed = update_needed(version, args.helper)
    result = {"version": version, "tag": tag, "needed": str(needed).lower()}
    print(json.dumps(result))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            for key, value in result.items():
                print(f"{key}={value}", file=output)


if __name__ == "__main__":
    main()
