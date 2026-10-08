#!/usr/bin/env python3
"""Resolve a source-bound core dependency lock, reusable across build jobs."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

from build_core import RECIPE, ROOT, download, json_url, recipe_hash, request, source_commit
from toolchain import resolve_go, validate_go_lock

GO_CATALOG = "https://go.dev/dl/?mode=json&include=all"
REPOSITORY = "thetapilla/tailscale-koolshare"


def lock_hash(lock):
    return hashlib.sha256(b"tailscale-core-lock-v1\0" + json.dumps(
        lock, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_lock(lock, check_recipe=False):
    fields = {"schema", "version", "build", "source", "go", "upstream_toolchain_rev", "recipe_source_sha256"}
    if not isinstance(lock, dict) or set(lock) != fields or type(lock["schema"]) is not int or lock["schema"] != 1:
        raise ValueError("invalid core dependency lock")
    if not isinstance(lock["version"], str) or not re.fullmatch(r"\d+\.\d+\.\d+", lock["version"]) or int(lock["version"].split(".")[1]) % 2:
        raise ValueError("lock must identify a stable core version")
    if not isinstance(lock["build"], str) or not re.fullmatch(r"r[1-9]\d{0,5}", lock["build"]):
        raise ValueError("invalid core build identity")
    source = lock["source"]
    if not isinstance(source, dict) or set(source) != {"commit", "url", "sha256"}:
        raise ValueError("invalid source lock")
    for value, length in ((source["commit"], 40), (source["sha256"], 64), (lock["recipe_source_sha256"], 64)):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{" + str(length) + r"}", value):
            raise ValueError("invalid dependency provenance hash")
    if source["url"] != "https://codeload.github.com/tailscale/tailscale/tar.gz/" + source["commit"]:
        raise ValueError("untrusted source archive URL")
    revision = lock["upstream_toolchain_rev"]
    if revision is not None and (not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision)):
        raise ValueError("invalid upstream toolchain revision")
    validate_go_lock(lock["go"])
    if RECIPE.get("go_policy") != "upstream-stable-official":
        raise ValueError("unsupported Go dependency policy")
    if check_recipe and lock["recipe_source_sha256"] != recipe_hash():
        raise ValueError("locked recipe differs from current sources; use the original recipe or a new build number")
    return lock


def source_requirements(archive, version, commit):
    with tarfile.open(archive) as source:
        def read(name, optional=False):
            try:
                item = source.getmember("tailscale-" + commit + "/" + name)
            except KeyError:
                if optional:
                    return None
                raise ValueError("source is missing " + name) from None
            if not item.isfile() or item.size > 1024 * 1024:
                raise ValueError("invalid source requirement file: " + name)
            return source.extractfile(item).read().decode("utf-8").strip()
        if read("VERSION.txt") != version:
            raise ValueError("source VERSION.txt disagrees with release tag")
        return read("go.mod"), read("go.toolchain.version", True), read("go.toolchain.rev", True)


def published_lock(version, build, helper):
    # Published metadata is trusted only when its complete lock matches the
    # recipe digest authenticated by both signed architecture descriptors.
    from publish_core import release_by_tag, verify_manifest
    tag = f"core-v{version}-{build}"
    release = release_by_tag(tag)
    if release is None:
        return None
    if helper is None or not Path(helper).is_file():
        raise ValueError("a verifier is required to reuse a published dependency lock")
    if release.get("draft"):
        with tempfile.TemporaryDirectory(prefix="core-lock-") as temporary:
            subprocess.run(["gh", "release", "download", tag, "--repo", REPOSITORY, "--dir", temporary,
                            "--pattern", "manifest.json", "--pattern", "build-metadata.json"], check=True)
            envelope = Path(temporary, "manifest.json").read_bytes()
            metadata = json.loads(Path(temporary, "build-metadata.json").read_bytes())
    else:
        base = f"https://github.com/{REPOSITORY}/releases/download/{tag}/"
        with request(base + "manifest.json") as response:
            envelope = response.read(65537)
        with request(base + "build-metadata.json") as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("published metadata exceeds size limit")
        metadata = json.loads(raw)
    descriptors = verify_manifest(envelope, helper=helper)
    if metadata.get("schema") != 2 or "dependency_lock" not in metadata:
        raise ValueError("published core predates dependency locks; reproduce it using its original source checkout")
    lock = validate_lock(metadata["dependency_lock"], check_recipe=True)
    for descriptor in descriptors.values():
        if (descriptor["version"], descriptor["build"], descriptor["source_commit"], descriptor["recipe_sha256"]) != (
                version, build, lock["source"]["commit"], lock_hash(lock)):
            raise ValueError("published dependency lock does not match its signed recipe")
    return lock


def resolve(version, commit, path, helper=None, reuse_published=True):
    path = Path(path)
    if source_commit(version) != commit:
        raise ValueError("upstream source tag changed after version discovery")
    if version == RECIPE["initial_version"] and commit != RECIPE["initial_source_commit"]:
        raise ValueError("pinned upstream source tag moved")
    lock = published_lock(version, RECIPE["build"], helper) if reuse_published else None
    cached = validate_lock(json.loads(path.read_text()), check_recipe=True) if path.exists() else None
    if lock is not None and cached is not None and lock != cached:
        raise ValueError("cached dependency lock differs from the signed published lock")
    lock = lock or cached
    if lock is not None:
        if (lock["version"], lock["build"], lock["source"]["commit"]) != (version, RECIPE["build"], commit):
            raise ValueError("cached dependency lock has a different source or release identity")
    else:
        archive = ROOT / ".cache/downloads" / ("tailscale-" + commit + ".tar.gz")
        url = "https://codeload.github.com/tailscale/tailscale/tar.gz/" + commit
        expected = RECIPE["initial_source_sha256"] if version == RECIPE["initial_version"] else None
        # Before the first lock there is no trusted digest for this archive.
        # Fetch it afresh instead of accepting an unverified local cache entry.
        if expected is None:
            archive.unlink(missing_ok=True)
        digest = download(url, archive, expected)
        go_mod, preferred, revision = source_requirements(archive, version, commit)
        lock = {"schema": 1, "version": version, "build": RECIPE["build"],
                "source": {"commit": commit, "url": url, "sha256": digest},
                "go": resolve_go(go_mod, preferred, json_url(GO_CATALOG)),
                "upstream_toolchain_rev": revision, "recipe_source_sha256": recipe_hash()}
        validate_lock(lock, check_recipe=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_text(json.dumps(lock, sort_keys=True, indent=2) + "\n")
    temporary.replace(path)
    return lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "build/core-lock.json")
    parser.add_argument("--helper", type=Path, default=ROOT / "build/tsks-helper")
    args = parser.parse_args()
    lock = resolve(args.version, args.source_commit, args.output, args.helper)
    print(json.dumps({"version": lock["version"], "build": lock["build"], "source_commit": lock["source"]["commit"],
                      "go_version": lock["go"]["version"], "recipe_sha256": lock_hash(lock)}))


if __name__ == "__main__":
    main()
