import copy
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
from build_core import RECIPE, recipe_hash
from package import PLATFORMS, build_packages
from release_core import validated_core
from resolve_core import lock_hash


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
        (self.root / "tools").mkdir()
        (self.root / "tools/bundled_core.json").write_text('{"version":"1.102.4","build":"r1"}\n')
        plugin = self.root / "plugin"
        (plugin / "scripts").mkdir(parents=True)
        (plugin / "webs").mkdir()
        (plugin / "res").mkdir()
        (plugin / "init.d").mkdir()
        (plugin / "scripts/tailscale_config").write_text("#!/bin/sh\nexit 0\n")
        (plugin / "init.d/S96tailscale.sh").write_text("#!/bin/sh\nexit 0\n")
        (plugin / "install.sh").write_text("#!/bin/sh\nexit 0\n")
        self.page = '<script src="/res/tailscale3.js?v=3.0.0"></script>'
        (plugin / "webs/Module_tailscale.asp").write_text(self.page)
        (plugin / "res/tailscale3.js").write_text("window.fixture = 1;\n")
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

    def test_host_guard_rejects_protected_text_even_in_comments(self):
        for token in ("detect_package", "ks_tar_install"):
            for content in (f"#!/bin/sh\n# Reference: {token}.sh\n", f"#!/bin/sh\necho {token}\n"):
                with self.subTest(token=token, content=content):
                    (self.root / "plugin/install.sh").write_text(content)
                    with self.assertRaisesRegex(ValueError, "software-center installer guard"):
                        self.packages()
                    self.assertFalse(self.out.exists())

    def test_same_version_replaces_canonical_archives(self):
        paths = self.packages()
        first = {path.name: path.read_bytes() for path in paths}
        (self.root / "plugin/webs/Module_tailscale.asp").write_text(self.page + "Updated help text")
        paths = self.packages()
        expected = {f"tailscale_3.0.0_{name}.tar.gz" for name in ("universal", *PLATFORMS)}
        self.assertEqual({path.name for path in paths}, expected)
        self.assertEqual({path.name for path in self.out.iterdir()}, expected | {"SHA256SUMS"})
        for path in paths:
            self.assertNotEqual(path.read_bytes(), first[path.name])
            with tarfile.open(path) as tar:
                self.assertEqual(tar.extractfile("tailscale/version").read(), b"3.0.0\n")
                installer = tar.extractfile("tailscale/install.sh").read()
                self.assertNotIn(b"detect_package", installer)
                self.assertNotIn(b"ks_tar_install", installer)

    def test_same_version_script_change_refreshes_browser_cache(self):
        self.packages()
        with self.open("hnd") as tar:
            first = tar.extractfile("tailscale/webs/Module_tailscale.asp").read()
        script = self.root / "plugin/res/tailscale3.js"
        script.write_text("window.fixture = 2;\n")
        self.packages()
        digest = hashlib.sha256(script.read_bytes()).hexdigest()[:16]
        for platform in ("universal", *PLATFORMS):
            with self.open(platform) as tar:
                page = tar.extractfile("tailscale/webs/Module_tailscale.asp").read()
                self.assertNotEqual(first, page)
                self.assertIn(f"/res/tailscale3.js?v=3.0.0-{digest}".encode(), page)
                self.assertEqual(tar.extractfile("tailscale/version").read(), b"3.0.0\n")
        self.assertEqual((self.root / "plugin/webs/Module_tailscale.asp").read_text(), self.page)

    def test_missing_or_duplicate_script_reference_rejects_package(self):
        for page in ("missing script", self.page * 2):
            (self.root / "plugin/webs/Module_tailscale.asp").write_text(page)
            with self.assertRaisesRegex(ValueError, "exactly once"):
                self.packages()

    def test_rejects_wrong_architecture(self):
        (self.root / "build/helpers/arm/tsks-helper").write_bytes(elf("arm64"))
        with self.assertRaisesRegex(ValueError, "architecture mismatch"):
            self.packages()

    def test_rejects_core_that_differs_from_bundle_pin(self):
        (self.root / "tools/bundled_core.json").write_text('{"version":"1.104.1","build":"r1"}\n')
        with self.assertRaisesRegex(ValueError, "bundled core pin"):
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


class CoreReleaseProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        version = "1.27.1"
        lock = {"schema": 1, "version": "1.104.1", "build": "r1",
                "source": {"commit": "a" * 40,
                           "url": "https://codeload.github.com/tailscale/tailscale/tar.gz/" + "a" * 40,
                           "sha256": "b" * 64},
                "go": {"version": version,
                       "requirements": {"go": version, "toolchain": None, "upstream": version},
                       "archives": {}},
                "upstream_toolchain_rev": None, "recipe_source_sha256": recipe_hash()}
        for arch in ("amd64", "arm64"):
            filename = f"go{version}.linux-{arch}.tar.gz"
            lock["go"]["archives"][arch] = {
                "filename": filename, "url": "https://go.dev/dl/" + filename,
                "sha256": "c" * 64, "size": 1234}
        self.metadata = {"schema": 2, "version": lock["version"], "build": lock["build"],
                         "source_commit": lock["source"]["commit"],
                         "source_sha256": lock["source"]["sha256"],
                         "go_version": version, "recipe_source_sha256": lock["recipe_source_sha256"],
                         "recipe_sha256": lock_hash(lock), "dependency_lock": lock,
                         "upx_version": RECIPE["upx_version"], "tags": list(RECIPE["tags"])}
        for arch in ("arm", "arm64"):
            core = self.root / "build/cores" / arch
            core.mkdir(parents=True)
            binary = elf(arch)
            (core / "tailscale.combined").write_bytes(binary)
            record = {key: self.metadata[key] for key in
                      ("version", "build", "source_commit", "source_sha256", "recipe_sha256", "go_version")}
            record.update(arch=arch, binary_size=len(binary), binary_sha256=hashlib.sha256(binary).hexdigest())
            (core / "build.json").write_text(json.dumps(record))

    def test_complete_dependency_lock_binds_both_architectures(self):
        for arch in ("arm", "arm64"):
            binary, size, digest = validated_core(self.root, arch, self.metadata)
            self.assertEqual(binary.read_bytes(), elf(arch))
            self.assertEqual(size, len(elf(arch)))
            self.assertEqual(digest, hashlib.sha256(elf(arch)).hexdigest())

    def test_new_metadata_requires_lock_and_cannot_downgrade_its_schema(self):
        for lock in (None, [], "unresolved"):
            metadata = dict(self.metadata, dependency_lock=lock)
            with self.subTest(lock=lock), self.assertRaisesRegex(ValueError, "requires a dependency lock"):
                validated_core(self.root, "arm", metadata)
        metadata = dict(self.metadata)
        del metadata["dependency_lock"]
        with self.assertRaisesRegex(ValueError, "requires a dependency lock"):
            validated_core(self.root, "arm", metadata)
        for schema in (None, 1, 3, True):
            metadata = dict(self.metadata)
            if schema is None:
                del metadata["schema"]
            else:
                metadata["schema"] = schema
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                validated_core(self.root, "arm", metadata)

    def test_metadata_identity_source_and_toolchain_must_match_lock(self):
        changes = {"version": "1.104.2", "build": "r2", "source_commit": "d" * 40,
                   "source_sha256": "d" * 64, "go_version": "1.26.6",
                   "recipe_source_sha256": "d" * 64, "recipe_sha256": "d" * 64}
        for field, value in changes.items():
            metadata = dict(self.metadata, **{field: value})
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "differs from its dependency lock"):
                validated_core(self.root, "arm", metadata)

    def test_signed_recipe_hash_covers_source_and_toolchain_archive(self):
        for field in ("source", "go"):
            metadata = copy.deepcopy(self.metadata)
            if field == "source":
                metadata["dependency_lock"]["source"]["sha256"] = "e" * 64
                metadata["source_sha256"] = "e" * 64
            else:
                metadata["dependency_lock"]["go"]["archives"]["amd64"]["sha256"] = "e" * 64
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "differs from its dependency lock"):
                validated_core(self.root, "arm", metadata)

    def test_metadata_must_describe_the_locked_compressor_and_tags(self):
        for field, value in (("upx_version", "4.2.0"), ("tags", [])):
            metadata = dict(self.metadata, **{field: value})
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "locked build recipe"):
                validated_core(self.root, "arm", metadata)

    def test_stale_recipe_source_is_rejected_even_with_recomputed_lock_hash(self):
        metadata = copy.deepcopy(self.metadata)
        metadata["dependency_lock"]["recipe_source_sha256"] = "f" * 64
        metadata["recipe_source_sha256"] = "f" * 64
        metadata["recipe_sha256"] = lock_hash(metadata["dependency_lock"])
        with self.assertRaises(ValueError):
            validated_core(self.root, "arm", metadata)

    def test_architecture_record_must_include_actual_source_and_go_version(self):
        for arch in ("arm", "arm64"):
            path = self.root / "build/cores" / arch / "build.json"
            original = json.loads(path.read_text())
            for field, value in (("go_version", "1.26.6"), ("source_sha256", "d" * 64)):
                for missing in (False, True):
                    record = dict(original)
                    if missing:
                        del record[field]
                    else:
                        record[field] = value
                    path.write_text(json.dumps(record))
                    with self.subTest(arch=arch, field=field, missing=missing), self.assertRaisesRegex(ValueError, "build provenance"):
                        validated_core(self.root, arch, self.metadata)
            path.write_text(json.dumps(original))

    def test_legacy_schema_one_release_metadata_remains_supported(self):
        for schema in (None, 1):
            metadata = {key: self.metadata[key] for key in ("version", "build", "source_commit", "recipe_sha256")}
            if schema is not None:
                metadata["schema"] = schema
            with self.subTest(schema=schema):
                validated_core(self.root, "arm", metadata)


if __name__ == "__main__":
    unittest.main()
