#!/usr/bin/env python3
"""Make deterministic core archives and a signed manifest; never publishes."""
import argparse
import datetime
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from artifact_utils import MAX_BINARY, archive_tree, check_elf, sha256

ROOT = Path(__file__).resolve().parents[1]


def validated_core(root, arch, metadata):
    binary = root / "build/cores" / arch / "tailscale.combined"
    size, digest = check_elf(binary, arch, MAX_BINARY)
    built = json.loads((binary.parent / "build.json").read_text())
    if any(built.get(key) != metadata[key] for key in ("version", "build", "source_commit", "recipe_sha256")):
        raise ValueError("architecture build provenance differs from release metadata: " + arch)
    if built.get("arch") != arch or built.get("binary_size") != size or built.get("binary_sha256") != digest:
        raise ValueError("architecture binary differs from its successful build record: " + arch)
    return binary, size, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", type=Path, default=ROOT / "build/tsks-helper")
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--public-key", type=Path, default=ROOT / "plugin/release.pub")
    parser.add_argument("--created-at", help="fixed RFC3339 timestamp when reproducing a release")
    args = parser.parse_args()
    meta = json.loads((ROOT / "build/core-build.json").read_text())
    version, build = meta["version"], meta["build"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or not re.fullmatch(r"r[1-9]\d*", build):
        raise ValueError("invalid release identity")
    cores = {arch: validated_core(ROOT, arch, meta) for arch in ("arm", "arm64")}
    out = ROOT / "build/core-release"
    out.mkdir(exist_ok=True)
    for previous_archive in out.glob("tailscale-core_*.tar.gz"):
        previous_archive.unlink()
    artifacts = {}
    for arch in ("arm", "arm64"):
        binary, size, digest = cores[arch]
        filename = f"tailscale-core_{version}_{build}_{arch}.tar.gz"
        archive = out / filename
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as tmp:
            copy = Path(tmp) / "tailscale.combined"
            shutil.copyfile(binary, copy)
            copy.chmod(0o755)
            archive_tree(tmp, archive, prefix="")
        artifacts[arch] = {"url": f"https://github.com/thetapilla/tailscale-koolshare/releases/download/core-v{version}-{build}/{filename}",
                           "size": archive.stat().st_size, "sha256": sha256(archive),
                           "unpacked_size": size, "binary_sha256": digest}
    payload = {k: meta[k] for k in ("version", "build", "source_commit", "recipe_sha256")}
    payload.update(schema=1, channel="stable", artifacts=artifacts,
                   created_at=args.created_at or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    (out / "payload.json").write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    subprocess.run([str(args.helper), "sign", str(out / "payload.json"), str(args.key), str(out / "manifest.json")], check=True)
    for arch in ("arm", "arm64"):
        descriptor = subprocess.check_output([str(args.helper), "verify", str(out / "manifest.json"), str(args.public_key), arch])
        (ROOT / "build/cores" / arch / "descriptor.json").write_bytes(descriptor)
    (out / "SHA256SUMS").write_text("".join(f"{sha256(p)}  {p.name}\n" for p in sorted(out.glob("tailscale-core_*.tar.gz"))))
    shutil.copyfile(ROOT / "build/core-build.json", out / "build-metadata.json")
    print(out)


if __name__ == "__main__":
    main()
