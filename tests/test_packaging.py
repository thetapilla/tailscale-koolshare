import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from artifact_utils import archive_tree, check_elf
from package import PLATFORMS, build_packages
from release_core import validated_core


def elf(arch):
    data = bytearray(128)
    data[:6] = b"\x7fELF" + bytes([1 if arch == "arm" else 2, 1])
    struct.pack_into("<H", data, 18, 40 if arch == "arm" else 183)
    return bytes(data)


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.out = Path(self.temp.name) / "dist"
        self.root.mkdir()
        (self.root / "VERSION").write_text("3.0.0\n")
        plugin = self.root / "plugin"
        (plugin / "scripts").mkdir(parents=True)
        (plugin / "webs").mkdir()
        (plugin / "init.d").mkdir()
        (plugin / "scripts/tailscale_config").write_text("#!/bin/sh\nexit 0\n")
        (plugin / "init.d/S96tailscale.sh").write_text("#!/bin/sh\nexit 0\n")
        (plugin / "install.sh").write_text("#!/bin/sh\nexit 0\n")
        (plugin / "webs/Module_tailscale.asp").write_text("3.0.0")
        (plugin / "version").write_text("old-source-version\n")
        (plugin / "release.pub").write_text("ab" * 32 + "\n")
        for arch in ("arm", "arm64"):
            core = self.root / "build/cores" / arch
            helper = self.root / "build/helpers" / arch
            core.mkdir(parents=True)
            helper.mkdir(parents=True)
            binary = elf(arch)
            (core / "tailscale.combined").write_bytes(binary)
            (helper / "tsks-helper").write_bytes(binary)
            descriptor = {"arch": arch, "version": "1.102.4", "build": "r1", "source_commit": "a" * 40,
                          "recipe_sha256": "b" * 64, "binary_sha256": hashlib.sha256(binary).hexdigest(),
                          "unpacked_size": len(binary)}
            (core / "descriptor.json").write_text(json.dumps(descriptor))
        (self.root / "build/core-release").mkdir()
        (self.root / "build/core-release/manifest.json").write_text('{"payload":"fixture","signature":"fixture"}\n')

    def tearDown(self):
        self.temp.cleanup()

    def packages(self):
        return build_packages(self.root, self.out)

    def open(self, platform):
        return tarfile.open(self.out / f"tailscale_3.0.0_{platform}.tar.gz")

    def test_six_packages_architecture_selection_matches_universal(self):
        self.assertEqual(len(self.packages()), 6)
        with self.open("universal") as universal:
            self.assertEqual(universal.extractfile("tailscale/.valid").read().decode().splitlines(), list(PLATFORMS))
            for platform, arch in PLATFORMS.items():
                with self.open(platform) as single:
                    self.assertEqual(single.extractfile("tailscale/.valid").read().decode(), platform + "\n")
                    for name in ("tailscale.combined", "tsks-helper", "descriptor.json"):
                        path = f"tailscale/payload/{arch}/{name}"
                        self.assertEqual(single.extractfile(path).read(), universal.extractfile(path).read())
                    wrong_arch = "arm64" if arch == "arm" else "arm"
                    self.assertFalse(any(f"/payload/{wrong_arch}/" in name for name in single.getnames()))

    def test_deterministic_archives_and_original_inputs_unchanged(self):
        paths = self.packages()
        first = {path.name: path.read_bytes() for path in paths}
        for file in (self.root / "plugin").rglob("*"):
            if file.is_file():
                file.touch()
        self.packages()
        self.assertEqual(first, {path.name: path.read_bytes() for path in paths})
        self.assertEqual((self.root / "plugin/version").read_text(), "old-source-version\n")

    def test_layout_modes_version_and_checksums(self):
        self.packages()
        with self.open("universal") as tar:
            for member in tar:
                self.assertTrue(member.name == "tailscale" or member.name.startswith("tailscale/"))
                self.assertEqual((member.uid, member.gid, member.mtime), (0, 0, 0))
                self.assertFalse(member.mode & 0o022)
            self.assertEqual(tar.extractfile("tailscale/version").read(), b"3.0.0\n")
            self.assertEqual(tar.getmember("tailscale/scripts/tailscale_config").mode, 0o755)
            self.assertEqual(tar.getmember("tailscale/install.sh").mode, 0o755)
            self.assertEqual(tar.getmember("tailscale/payload/arm/tsks-helper").mode, 0o755)
            self.assertEqual(tar.getmember("tailscale/release.pub").mode, 0o644)
            for line in tar.extractfile("tailscale/manifest.sha256").read().decode().splitlines():
                digest, name = line.split("  ", 1)
                self.assertEqual(hashlib.sha256(tar.extractfile("tailscale/" + name).read()).hexdigest(), digest)

    def test_rejects_wrong_architecture(self):
        (self.root / "build/helpers/arm/tsks-helper").write_bytes(elf("arm64"))
        with self.assertRaisesRegex(ValueError, "architecture mismatch"):
            self.packages()

    def test_rejects_changed_binary(self):
        with (self.root / "build/cores/arm/tailscale.combined").open("ab") as file:
            file.write(b"changed")
        with self.assertRaisesRegex(ValueError, "descriptor"):
            self.packages()

    def test_rejects_mixed_recipes(self):
        path = self.root / "build/cores/arm64/descriptor.json"
        descriptor = json.loads(path.read_text())
        descriptor["recipe_sha256"] = "d" * 64
        path.write_text(json.dumps(descriptor))
        with self.assertRaisesRegex(ValueError, "different source recipes"):
            self.packages()

    def test_safe_relative_symlink_preserved(self):
        (self.root / "plugin/scripts/alias").symlink_to("tailscale_config")
        archive = Path(self.temp.name) / "relative-symlink.tar.gz"
        archive_tree(self.root / "plugin", archive)
        with tarfile.open(archive) as tar:
            member = tar.getmember("tailscale/scripts/alias")
            self.assertTrue(member.issym())
            self.assertEqual(member.linkname, "tailscale_config")

    def test_rejects_escaping_symlink(self):
        (self.root / "plugin/scripts/escape").symlink_to("/etc/passwd")
        with self.assertRaisesRegex(ValueError, "package symlinks"):
            self.packages()

    def test_core_archive_exactly_one_executable(self):
        source = Path(self.temp.name) / "core"
        source.mkdir()
        (source / "tailscale.combined").write_bytes(elf("arm"))
        (source / "tailscale.combined").chmod(0o755)
        dest = Path(self.temp.name) / "core.tar.gz"
        archive_tree(source, dest, prefix="")
        with tarfile.open(dest) as tar:
            self.assertEqual(tar.getnames(), ["tailscale.combined"])
            self.assertTrue(tar.getmembers()[0].isfile())
            self.assertEqual(tar.getmembers()[0].mode, 0o755)

    def test_partial_build_cannot_sign_stale_architecture(self):
        meta = {"version": "1.104.0", "build": "r1", "source_commit": "a" * 40, "recipe_sha256": "b" * 64}
        core = self.root / "build/cores/arm"
        old = dict(meta, version="1.102.4", arch="arm", binary_size=128,
                   binary_sha256=hashlib.sha256(elf("arm")).hexdigest())
        (core / "build.json").write_text(json.dumps(old))
        with self.assertRaisesRegex(ValueError, "build provenance"):
            validated_core(self.root, "arm", meta)

    def test_release_binds_binary_to_successful_build_record(self):
        meta = {"version": "1.102.4", "build": "r1", "source_commit": "a" * 40, "recipe_sha256": "b" * 64}
        core = self.root / "build/cores/arm"
        record = dict(meta, arch="arm", binary_size=128, binary_sha256="0" * 64)
        (core / "build.json").write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "successful build record"):
            validated_core(self.root, "arm", meta)


if __name__ == "__main__":
    unittest.main()
