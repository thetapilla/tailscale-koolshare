import copy
import hashlib
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import toolchain


def release(version, archives=None):
    return {"version": "go" + version, "stable": True, "files": [
        {"version": "go" + version, "filename": f"go{version}.linux-{arch}.tar.gz",
         "os": "linux", "arch": arch, "kind": "archive",
         "sha256": hashlib.sha256(archives[arch]).hexdigest() if archives else "a" * 64,
         "size": len(archives[arch]) if archives else 1234}
        for arch in toolchain.ARCHES]}


class GoResolutionTests(unittest.TestCase):
    def setUp(self):
        self.catalog = [release(version) for version in ("1.24.0", "1.26.6", "1.27.1", "1.27.2", "1.28.0")]

    def test_new_source_requirement_selects_newer_exact_toolchain(self):
        lock = toolchain.resolve_go("module tailscale.com\n\ngo 1.27.1\n", None, self.catalog)
        self.assertEqual(lock["version"], "1.27.1")
        self.assertEqual(lock["requirements"], {"go": "1.27.1", "toolchain": None, "upstream": None})
        self.assertEqual(lock["archives"]["arm64"]["url"], "https://go.dev/dl/go1.27.1.linux-arm64.tar.gz")

    def test_highest_source_suggestion_wins_without_tracking_latest(self):
        lock = toolchain.resolve_go("go 1.26.6\ntoolchain go1.27.1 // build preference\n", "1.27.2\n", self.catalog)
        self.assertEqual(lock["version"], "1.27.2")
        self.assertEqual(lock["requirements"], {"go": "1.26.6", "toolchain": "1.27.1", "upstream": "1.27.2"})
        lock = toolchain.resolve_go("go 1.26.6\ntoolchain go1.27.2\n", "1.27.1", self.catalog)
        self.assertEqual(lock["version"], "1.27.2")

    def test_old_source_without_suggestion_uses_its_minimum(self):
        lock = toolchain.resolve_go("module example.org/project\ngo 1.24\n", "", self.catalog)
        self.assertEqual(lock["version"], "1.24.0")

    def test_default_is_no_toolchain_suggestion(self):
        lock = toolchain.resolve_go("go 1.26.6\ntoolchain default\n", None, self.catalog)
        self.assertEqual(lock["version"], "1.26.6")
        self.assertIsNone(lock["requirements"]["toolchain"])

    def test_rejects_unstable_malformed_or_ambiguous_source_requirements(self):
        for go_mod, upstream in (
            ("go 1.27rc1", None), ("go 1.26.6\ntoolchain go1.27rc1", None),
            ("go 1.26.6", "devel go1.28"), ("go 1.26.6", "1.27.1;echo injected"),
            ("go 1.26.6\ngo 1.27.1", None), ("module example.org/project", None),
            ("go 1.26.6\ntoolchain go1.27.1\ntoolchain go1.27.2", None),
            ("go 1.26.6 extra", None), ("go 01.26.6", None),
        ):
            with self.subTest(go_mod=go_mod, upstream=upstream), self.assertRaises(ValueError):
                toolchain.resolve_go(go_mod, upstream, self.catalog)

    def test_missing_exact_release_does_not_substitute_newer_release(self):
        with self.assertRaisesRegex(ValueError, "official stable release"):
            toolchain.resolve_go("go 1.27.0", None, self.catalog)

    def test_rejects_unstable_or_duplicate_catalog_release(self):
        for catalog in ([dict(release("1.27.1"), stable=False)], [release("1.27.1"), release("1.27.1")]):
            with self.subTest(catalog=catalog), self.assertRaises(ValueError):
                toolchain.resolve_go("go 1.27.1", None, catalog)

    def test_rejects_catalog_architecture_hash_filename_and_size_errors(self):
        mutations = (
            lambda item: item["files"].pop(),
            lambda item: item["files"].append(copy.deepcopy(item["files"][0])),
            lambda item: item["files"][0].update(sha256="bad"),
            lambda item: item["files"][0].update(filename="../../go.tar.gz"),
            lambda item: item["files"][0].update(size=True),
            lambda item: item["files"][0].update(size=0),
            lambda item: item["files"][0].update(version="go1.26.6"),
        )
        for mutate in mutations:
            item = release("1.27.1")
            mutate(item)
            with self.subTest(item=item), self.assertRaises(ValueError):
                toolchain.resolve_go("go 1.27.1", None, [item])

    def test_lock_rejects_url_rewrite_and_requirements_drift(self):
        base = toolchain.resolve_go("go 1.27.1", None, self.catalog)
        for url in ("http://go.dev/dl/go1.27.1.linux-amd64.tar.gz", "https://evil.test/go1.27.1.linux-amd64.tar.gz",
                    "https://go.dev/dl/go1.27.1.linux-amd64.tar.gz?redirect=evil", "https://go.dev.evil.test/dl/go1.27.1.linux-amd64.tar.gz"):
            lock = copy.deepcopy(base)
            lock["archives"]["amd64"]["url"] = url
            with self.subTest(url=url), self.assertRaises(ValueError):
                toolchain.validate_go_lock(lock)
        lock = copy.deepcopy(base)
        lock["requirements"]["upstream"] = "1.28.0"
        with self.assertRaises(ValueError):
            toolchain.validate_go_lock(lock)

    def test_cache_identity_binds_role_host_version_and_archive_digests(self):
        lock = toolchain.resolve_go("go 1.27.1", None, self.catalog)
        original = toolchain.toolchain_dir(Path("/cache"), "core", lock, "amd64")
        self.assertNotEqual(original, toolchain.toolchain_dir("/cache", "helper", lock, "amd64"))
        self.assertNotEqual(original, toolchain.toolchain_dir("/cache", "core", lock, "arm64"))
        changed = copy.deepcopy(lock)
        changed["archives"]["arm64"]["sha256"] = "b" * 64
        self.assertNotEqual(original, toolchain.toolchain_dir("/cache", "core", changed, "amd64"))
        with self.assertRaises(ValueError):
            toolchain.toolchain_dir("/cache", "../core", lock, "amd64")


def go_archive(version, arch):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content, mode in (("go/VERSION", ("go" + version + "\ntime test\n").encode(), 0o644),
                                    ("go/bin/go", ("compiler " + arch).encode(), 0o755),
                                    ("go/pkg/tool/compile", b"compiler tool", 0o755)):
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(content), mode
            archive.addfile(member, io.BytesIO(content))
    return buffer.getvalue()


class GoProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name)
        self.archives = {arch: go_archive("1.27.1", arch) for arch in toolchain.ARCHES}
        self.lock = toolchain.resolve_go("go 1.27.1", None, [release("1.27.1", self.archives)])
        self.downloads, self.extractions = [], []

    def download(self, url, destination, expected):
        arch = "arm64" if "arm64" in url else "amd64"
        self.assertEqual(hashlib.sha256(self.archives[arch]).hexdigest(), expected)
        self.downloads.append(url)
        destination.write_bytes(self.archives[arch])

    def extract(self, archive, destination):
        self.assertFalse(destination.exists())
        self.extractions.append(destination)
        destination.mkdir(parents=True)
        with tarfile.open(archive) as source:
            source.extractall(destination, filter="data")

    def provision(self, **callbacks):
        return toolchain.provision_go(self.lock, "amd64", self.cache, "core",
                                      callbacks.get("download", self.download), callbacks.get("extract", self.extract))

    def test_reuses_complete_verified_toolchain(self):
        directory = self.provision()
        self.assertEqual((directory / "go/VERSION").read_text().splitlines()[0], "go1.27.1")
        self.assertEqual(directory, self.provision())
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(len(self.extractions), 1)

    def test_recovers_tampered_archive_and_extracted_compiler(self):
        directory = self.provision()
        archive = next((self.cache / "downloads").glob("*.tar.gz"))
        archive.write_bytes(b"damaged archive")
        (directory / "go/pkg/tool/compile").write_bytes(b"damaged executable")
        self.assertEqual(directory, self.provision())
        self.assertEqual((directory / "go/pkg/tool/compile").read_bytes(), b"compiler tool")
        self.assertEqual(len(self.downloads), 2)
        self.assertEqual(len(self.extractions), 2)

    def test_recovers_partial_extraction_without_overlaying_stale_files(self):
        directory = toolchain.toolchain_dir(self.cache, "core", self.lock, "amd64")
        directory.mkdir(parents=True)
        (directory / "stale").write_text("previous compiler")
        self.assertEqual(directory, self.provision())
        self.assertFalse((directory / "stale").exists())

    def test_failed_repair_keeps_previous_directory_intact(self):
        directory = self.provision()
        (directory / "go/VERSION").write_text("corrupted")

        def fail_extract(archive, destination):
            destination.mkdir(parents=True)
            raise RuntimeError("interrupted extraction")

        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.provision(extract=fail_extract)
        self.assertEqual((directory / "go/VERSION").read_text(), "corrupted")
        self.provision()
        self.assertEqual((directory / "go/VERSION").read_text().splitlines()[0], "go1.27.1")

    def test_rejects_bad_download_even_when_downloader_omits_hash_check(self):
        def unverified_download(url, destination, expected):
            destination.write_bytes(b"bad archive")

        with self.assertRaisesRegex(ValueError, "locked size or SHA-256"):
            self.provision(download=unverified_download)
        self.assertFalse(self.extractions)

    def test_rejects_archive_with_wrong_embedded_version(self):
        self.archives = {arch: go_archive("1.26.6", arch) for arch in toolchain.ARCHES}
        self.lock = toolchain.resolve_go("go 1.27.1", None, [release("1.27.1", self.archives)])
        with self.assertRaisesRegex(ValueError, "version differs"):
            self.provision()

    def test_roles_do_not_share_extracted_directories(self):
        core = self.provision()
        helper = toolchain.provision_go(self.lock, "amd64", self.cache, "helper", self.download, self.extract)
        self.assertNotEqual(core, helper)
        self.assertTrue((core / "go/bin/go").exists())
        self.assertTrue((helper / "go/bin/go").exists())
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(len(self.extractions), 2)


if __name__ == "__main__":
    unittest.main()
