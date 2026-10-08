#!/usr/bin/env python3
"""Prepare the plugin's pinned core from verified immutable release assets."""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from build_core import download

ROOT = Path(__file__).resolve().parents[1]


def bundle_identity(root=ROOT):
    value = json.loads((root / "tools/bundled_core.json").read_text())
    if set(value) != {"version", "build"} or not re.fullmatch(r"\d+\.\d+\.\d+", value["version"]) or int(value["version"].split(".")[1]) % 2 or not re.fullmatch(r"r[1-9]\d{0,5}", value["build"]):
        raise ValueError("invalid bundled core identity")
    return value


def prepare(root, helper, release_dir=None):
    root, helper = Path(root), Path(helper).resolve()
    identity = bundle_identity(root)
    base = f'https://github.com/thetapilla/tailscale-koolshare/releases/download/core-v{identity["version"]}-{identity["build"]}/'
    build = root / "build"
    build.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".bundle-", dir=build) as temporary:
        stage = Path(temporary)
        assets = stage / "assets"
        assets.mkdir()
        manifest = assets / "manifest.json"
        if release_dir:
            shutil.copyfile(Path(release_dir) / "manifest.json", manifest)
        else:
            download(base + "manifest.json", manifest)
        descriptors = {}
        for arch in ("arm", "arm64"):
            descriptor = json.loads(subprocess.check_output([str(helper), "verify", str(manifest), str(root / "plugin/release.pub"), arch]))
            if any(descriptor[key] != identity[key] for key in identity) or descriptor["arch"] != arch:
                raise ValueError("signed core differs from the bundled core pin")
            archive = assets / descriptor["url"].rsplit("/", 1)[1]
            if release_dir:
                shutil.copyfile(Path(release_dir) / archive.name, archive)
            else:
                download(descriptor["url"], archive, descriptor["sha256"])
            metadata = stage / (arch + ".json")
            metadata.write_text(json.dumps(descriptor))
            subprocess.run([str(helper), "extract", str(archive), str(metadata), str(stage / arch)], check=True)
            descriptors[arch] = descriptor
        if any(descriptors["arm"][key] != descriptors["arm64"][key] for key in ("version", "build", "source_commit", "recipe_sha256")):
            raise ValueError("bundled architectures have different signed provenance")
        (assets / "SHA256SUMS").write_text("".join(
            d["sha256"] + "  " + d["url"].rsplit("/", 1)[1] + "\n" for d in descriptors.values()))
        # All inputs are verified before changing any package build input.
        for arch in ("arm", "arm64"):
            target = build / "cores" / arch
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                shutil.rmtree(target)
            shutil.move(str(stage / arch), target)
        target = build / "core-release"
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(assets), target)
    return identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", type=Path, default=ROOT / "build/tsks-helper")
    parser.add_argument("--release-dir", type=Path, help="use local immutable release assets instead of downloading")
    args = parser.parse_args()
    print(json.dumps(prepare(ROOT, args.helper, args.release_dir)))


if __name__ == "__main__":
    main()
