"""Deterministic archives and binary validation shared by release tooling."""
import gzip
import hashlib
import io
from pathlib import Path
import struct
import tarfile

MAX_BINARY = 12 * 1024 * 1024
ARCH_MACHINE = {"arm": (1, 40), "arm64": (2, 183)}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_elf(path, arch, maximum=None):
    data = Path(path).read_bytes()
    if maximum and len(data) > maximum:
        raise ValueError(f"{path}: exceeds size gate {maximum}")
    if len(data) < 64 or data[:4] != b"\x7fELF" or data[5] != 1:
        raise ValueError(f"{path}: expected little-endian ELF")
    if (data[4], struct.unpack_from("<H", data, 18)[0]) != ARCH_MACHINE[arch]:
        raise ValueError(f"{path}: ELF architecture mismatch for {arch}")
    return len(data), hashlib.sha256(data).hexdigest()


def archive_tree(source, output, prefix="tailscale"):
    """Stable ordering, modes, uid/gid, timestamps, and gzip header."""
    source, output = Path(source), Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as tar:
            paths = [source, *sorted(source.rglob("*"))] if prefix else sorted(source.rglob("*"))
            for path in paths:
                rel = path.relative_to(source).as_posix()
                name = prefix if rel == "." else "/".join(p for p in (prefix, rel) if p)
                info = tarfile.TarInfo(name)
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = "root"
                if path.is_symlink():
                    target = path.readlink()
                    if target.is_absolute() or not path.resolve().is_relative_to(source.resolve()):
                        raise ValueError("unsafe package symlink: " + str(path))
                    info.type, info.mode, info.linkname = tarfile.SYMTYPE, 0o777, str(target)
                    tar.addfile(info)
                elif path.is_dir():
                    info.type, info.mode = tarfile.DIRTYPE, 0o755
                    tar.addfile(info)
                elif path.is_file():
                    data = path.read_bytes()
                    info.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
                else:
                    raise ValueError("unsupported package member: " + str(path))
