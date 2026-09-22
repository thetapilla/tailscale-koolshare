#!/usr/bin/env python3
"""Build the six installation archives from one verified set of inputs."""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess

from artifact_utils import MAX_BINARY, archive_tree, check_elf, sha256

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = {"hnd": "arm", "qca": "arm", "ipq32": "arm", "ipq64": "arm64", "mtk": "arm64"}
HOST_INSTALLER_BLOCKED_TEXT = (b"detect_package", b"ks_tar_install")


def check_host_installer(path):
    """Match the host's raw installer-text gate, including shell comments."""
    text = Path(path).read_bytes()
    for token in HOST_INSTALLER_BLOCKED_TEXT:
        if token in text:
            raise ValueError("software-center installer guard rejects text: " + token.decode())


def build_packages(root, out, public_key=None, helper=None, revision=None):
    root, out = Path(root), Path(out)
    version = (root / "VERSION").read_text().strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("invalid plugin version")
    if revision is not None and not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,31}", revision):
        raise ValueError("invalid package revision")
    check_host_installer(root / "plugin/install.sh")
    if any(path.is_symlink() for path in (root / "plugin").rglob("*")):
        raise ValueError("plugin package symlinks are not accepted by the installer")
    public_key = Path(public_key or root / "plugin/release.pub")
    if not re.fullmatch(r"[0-9a-fA-F]{64}\s*", public_key.read_text()):
        raise ValueError("release.pub must contain an Ed25519 public key in hex")
    manifest = root / "build/core-release/manifest.json"
    if not manifest.is_file():
        raise ValueError("signed core manifest is required")
    descriptors = {}
    for arch in ("arm", "arm64"):
        binary = root / "build/cores" / arch / "tailscale.combined"
        size, digest = check_elf(binary, arch, MAX_BINARY)
        descriptor = json.loads((binary.parent / "descriptor.json").read_text())
        if descriptor["arch"] != arch or descriptor["unpacked_size"] != size or descriptor["binary_sha256"] != digest:
            raise ValueError("binary does not match verified descriptor: " + arch)
        if helper:
            verified = json.loads(subprocess.check_output([str(helper), "verify", str(manifest), str(public_key), arch]))
            if verified != descriptor:
                raise ValueError("descriptor is not signed: " + arch)
        check_elf(root / "build/helpers" / arch / "tsks-helper", arch)
        descriptors[arch] = descriptor
    if any(descriptors["arm"][key] != descriptors["arm64"][key]
           for key in ("version", "build", "source_commit", "recipe_sha256")):
        raise ValueError("architecture payloads use different source recipes")
    result = []
    for platform in ("universal", *PLATFORMS):
        stage = root / "build/packages" / platform / "tailscale"
        if stage.exists():
            shutil.rmtree(stage)
        shutil.copytree(root / "plugin", stage, symlinks=True)
        check_host_installer(stage / "install.sh")
        (stage / "version").write_text(version + "\n")
        valid = list(PLATFORMS) if platform == "universal" else [platform]
        (stage / ".valid").write_text("\n".join(valid) + "\n")
        shutil.copyfile(public_key, stage / "release.pub")
        shutil.copyfile(manifest, stage / "release.json")
        arches = ("arm", "arm64") if platform == "universal" else (PLATFORMS[platform],)
        for arch in arches:
            payload = stage / "payload" / arch
            payload.mkdir(parents=True)
            for name, source in (("tailscale.combined", root / "build/cores" / arch),
                                 ("tsks-helper", root / "build/helpers" / arch),
                                 ("descriptor.json", root / "build/cores" / arch)):
                shutil.copyfile(source / name, payload / name)
                (payload / name).chmod(0o644 if name.endswith(".json") else 0o755)
        for file in stage.rglob("*"):
            if file.is_symlink():
                continue
            executable = (file.parent.name in ("scripts", "init.d") or file.name in ("install.sh", "uninstall.sh", "tailscale.combined", "tsks-helper"))
            file.chmod(0o755 if file.is_dir() or executable else 0o644)
        checksums = []
        for file in sorted(stage.rglob("*")):
            if file.is_file() and not file.is_symlink() and file.name != "manifest.sha256":
                checksums.append(f"{sha256(file)}  {file.relative_to(stage).as_posix()}\n")
        (stage / "manifest.sha256").write_text("".join(checksums))
        (stage / "manifest.sha256").chmod(0o644)
        suffix = "_" + revision if revision else ""
        archive = out / f"tailscale_{version}_{platform}{suffix}.tar.gz"
        archive_tree(stage, archive)
        result.append(archive)
    (out / "SHA256SUMS").write_text("".join(f"{sha256(path)}  {path.name}\n" for path in result))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT.parent / "dist")
    parser.add_argument("--public-key", type=Path, default=ROOT / "plugin/release.pub")
    parser.add_argument("--helper", type=Path, default=ROOT / "build/tsks-helper")
    parser.add_argument("--revision", help="optional installation-package revision; plugin version is unchanged")
    args = parser.parse_args()
    for path in build_packages(ROOT, args.output, args.public_key, args.helper, args.revision):
        print(path)


if __name__ == "__main__":
    main()
