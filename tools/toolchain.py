#!/usr/bin/env python3
"""Resolve official Go archives and provision immutable, isolated toolchains."""
import hashlib
import json
from pathlib import Path
import re
import tempfile


ARCHES = ("amd64", "arm64")
_VERSION = re.compile(r"1\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


def _version(value, language=False):
    if not isinstance(value, str):
        raise ValueError("Go version must be a stable numeric version")
    value = value.strip().removeprefix("go")
    if language and re.fullmatch(r"1\.(0|[1-9]\d*)", value):
        value += ".0"
    if not _VERSION.fullmatch(value):
        raise ValueError("Go version must be a stable numeric version")
    return value


def _key(version):
    return tuple(map(int, version.split(".")))


def _requirements(go_mod, upstream_version):
    if not isinstance(go_mod, str):
        raise ValueError("go.mod must be text")
    directives = {"go": [], "toolchain": []}
    for raw in go_mod.splitlines():
        line = raw.split("//", 1)[0].strip()
        words = line.split()
        if words and words[0] in directives:
            if len(words) != 2:
                raise ValueError("invalid Go toolchain directive")
            directives[words[0]].append(words[1])
    if len(directives["go"]) != 1 or len(directives["toolchain"]) > 1:
        raise ValueError("expected one go directive and at most one toolchain directive")
    suggested = directives["toolchain"][0] if directives["toolchain"] else None
    return {
        "go": _version(directives["go"][0], language=True),
        "toolchain": None if suggested in (None, "default") else _version(suggested),
        "upstream": None if upstream_version is None or upstream_version == "" else _version(upstream_version),
    }


def validate_go_lock(lock):
    """Validate a canonical lock without trusting its download URLs or hashes."""
    if not isinstance(lock, dict) or set(lock) != {"version", "requirements", "archives"}:
        raise ValueError("invalid Go lock fields")
    version = _version(lock["version"])
    if version != lock["version"]:
        raise ValueError("Go lock version must be canonical")
    requirements = lock["requirements"]
    if not isinstance(requirements, dict) or set(requirements) != {"go", "toolchain", "upstream"}:
        raise ValueError("invalid Go requirements")
    if requirements["go"] is None:
        raise ValueError("Go minimum is required")
    for value in requirements.values():
        if value is not None and _version(value) != value:
            raise ValueError("Go requirements must be canonical")
    if version != max((v for v in requirements.values() if v is not None), key=_key):
        raise ValueError("Go lock does not match the source requirements")
    archives = lock["archives"]
    if not isinstance(archives, dict) or set(archives) != set(ARCHES):
        raise ValueError("Go archives must cover linux amd64 and arm64")
    for arch in ARCHES:
        archive = archives[arch]
        if not isinstance(archive, dict) or set(archive) != {"filename", "url", "sha256", "size"}:
            raise ValueError("invalid Go archive fields")
        filename = f"go{version}.linux-{arch}.tar.gz"
        if archive["filename"] != filename or archive["url"] != "https://go.dev/dl/" + filename:
            raise ValueError("Go archive must use its exact official download URL")
        if not isinstance(archive["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", archive["sha256"]):
            raise ValueError("invalid Go archive SHA-256")
        if type(archive["size"]) is not int or archive["size"] <= 0:
            raise ValueError("invalid Go archive size")
    return lock


def resolve_go(go_mod, upstream_version, catalog):
    """Select the exact highest stable requirement using the official Go catalog.

    The caller fetches https://go.dev/dl/?mode=json&include=all and persists the
    result as a release input. Resolving again is not a substitute for that lock.
    """
    requirements = _requirements(go_mod, upstream_version)
    version = max((v for v in requirements.values() if v is not None), key=_key)
    if not isinstance(catalog, list):
        raise ValueError("Go download catalog must be a list")
    releases = [item for item in catalog if isinstance(item, dict) and item.get("version") == "go" + version]
    if len(releases) != 1 or releases[0].get("stable") is not True:
        raise ValueError("required Go version is not a unique official stable release: " + version)
    files = releases[0].get("files")
    if not isinstance(files, list):
        raise ValueError("official Go release has no archive list")
    archives = {}
    for arch in ARCHES:
        matches = [item for item in files if isinstance(item, dict) and item.get("os") == "linux"
                   and item.get("arch") == arch and item.get("kind") == "archive"]
        if len(matches) != 1:
            raise ValueError("required Go archive is missing or ambiguous: linux/" + arch)
        item = matches[0]
        if item.get("version") != "go" + version:
            raise ValueError("Go archive version differs from release")
        filename = item.get("filename")
        archives[arch] = {"filename": filename, "url": "https://go.dev/dl/" + str(filename),
                          "sha256": item.get("sha256"), "size": item.get("size")}
    return validate_go_lock({"version": version, "requirements": requirements, "archives": archives})


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def toolchain_dir(cache, role, lock, host):
    """Return a directory containing go/, isolated by role and complete lock."""
    validate_go_lock(lock)
    if not isinstance(role, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", role):
        raise ValueError("invalid Go toolchain role")
    if host not in ARCHES:
        raise ValueError("unsupported Go host architecture")
    digest = hashlib.sha256(_encoded(lock)).hexdigest()[:16]
    return Path(cache) / "toolchains" / role / ("go" + lock["version"]) / (host + "-" + digest)


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_valid(path, archive):
    return (not path.is_symlink() and path.is_file() and path.stat().st_size == archive["size"]
            and _sha256(path) == archive["sha256"])


def _tree_hash(root):
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative == ".toolchain.json":
            continue
        if path.is_symlink():
            value = [relative, "link", str(path.readlink())]
        elif path.is_file():
            value = [relative, "file", path.stat().st_mode & 0o777, _sha256(path)]
        elif path.is_dir():
            value = [relative, "dir"]
        else:
            raise ValueError("unexpected file type in Go toolchain")
        digest.update(_encoded(value) + b"\n")
    return digest.hexdigest()


def _check_extracted(root, version):
    executable, stamp = root / "go/bin/go", root / "go/VERSION"
    if (root / "go").is_symlink() or (root / "go/bin").is_symlink():
        raise ValueError("Go archive has an invalid executable directory")
    if executable.is_symlink() or not executable.is_file() or not executable.stat().st_mode & 0o111:
        raise ValueError("Go archive has no executable go/bin/go")
    if stamp.is_symlink() or not stamp.is_file():
        raise ValueError("extracted Go version differs from lock")
    lines = stamp.read_text().splitlines()
    if not lines or lines[0] != "go" + version:
        raise ValueError("extracted Go version differs from lock")


def _installed_valid(root, lock, host):
    try:
        if root.is_symlink() or not root.is_dir():
            return False
        _check_extracted(root, lock["version"])
        if (root / ".toolchain.json").is_symlink():
            return False
        marker = json.loads((root / ".toolchain.json").read_text())
        return marker == {"lock": lock, "host": host, "tree_sha256": _tree_hash(root)}
    except (OSError, ValueError, IndexError):
        return False


def provision_go(lock, host, cache, role, download, extract):
    """Verify archives and atomically provision go/ without overlay extraction.

    download(url, path, expected_sha256) and extract(archive, destination) are
    supplied by the caller. A cached extracted tree is checked before reuse;
    changed files or incomplete extraction trigger a fresh staging extraction.
    """
    target = toolchain_dir(cache, role, lock, host)
    archive = lock["archives"][host]
    downloads = Path(cache) / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    cached = downloads / (archive["sha256"][:16] + "-" + archive["filename"])
    if not _archive_valid(cached, archive):
        with tempfile.TemporaryDirectory(prefix=".go-download-", dir=downloads) as temporary:
            candidate = Path(temporary) / archive["filename"]
            download(archive["url"], candidate, archive["sha256"])
            if not _archive_valid(candidate, archive):
                raise ValueError("downloaded Go archive differs from its locked size or SHA-256")
            candidate.replace(cached)
    if _installed_valid(target, lock, host):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".go-extract-", dir=target.parent) as temporary:
        staging = Path(temporary) / "toolchain"
        extract(cached, staging)
        _check_extracted(staging, lock["version"])
        marker = {"lock": lock, "host": host, "tree_sha256": _tree_hash(staging)}
        (staging / ".toolchain.json").write_bytes(_encoded(marker) + b"\n")
        # A concurrent provisioner may already have completed the same lock.
        if _installed_valid(target, lock, host):
            return target
        if target.exists() or target.is_symlink():
            previous = Path(temporary) / "previous"
            target.rename(previous)
        try:
            staging.rename(target)
        except FileExistsError:
            if not _installed_valid(target, lock, host):
                raise
    return target
