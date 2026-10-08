#!/usr/bin/env python3
"""Check source/dependency identity across discovery, build, and release reuse."""
import copy
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import build_core
import publish_core
import resolve_core
import toolchain


VERSION = "1.104.1"
COMMIT = "a" * 40
REVISION = "b" * 40
RECIPE_HASH = "c" * 64


def source_archive(version=VERSION, commit=COMMIT, go="1.27.1", preferred="1.27.1", revision=REVISION):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        files = {"VERSION.txt": version, "go.mod": "module tailscale.com\n\ngo " + go + "\n"}
        if preferred is not None:
            files["go.toolchain.version"] = preferred
        if revision is not None:
            files["go.toolchain.rev"] = revision
        for name, text in files.items():
            data = text.encode()
            member = tarfile.TarInfo("tailscale-" + commit + "/" + name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return gzip.compress(buffer.getvalue(), mtime=0)


def go_catalog():
    return [{"version": "go" + version, "stable": True, "files": [
        {"version": "go" + version, "filename": f"go{version}.linux-{arch}.tar.gz",
         "os": "linux", "arch": arch, "kind": "archive", "sha256": "d" * 64, "size": 1024}
        for arch in ("amd64", "arm64")
    ]} for version in ("1.26.6", "1.27.1", "1.28.0")]


class CoreLockFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "build/core-lock.json"
        self.recipe = copy.deepcopy(build_core.RECIPE)
        self.archive = source_archive()
        self.downloads = []
        self.network = []
        for module in (build_core, resolve_core):
            self.patch(module, "ROOT", self.root)
            self.patch(module, "RECIPE", self.recipe)
            self.patch(module, "recipe_hash", return_value=RECIPE_HASH)
        self.source_commit = self.patch(resolve_core, "source_commit", return_value=COMMIT)
        self.catalog = self.patch(resolve_core, "json_url", side_effect=self.catalog_request)
        self.download = self.patch(resolve_core, "download", side_effect=self.download_source)
        self.no_raw_network = self.patch(build_core, "request", side_effect=AssertionError("unexpected network"))
        self.no_subprocess = self.patch(build_core.subprocess, "run", side_effect=AssertionError("unexpected process"))

    def patch(self, target, name, *args, **kwargs):
        patcher = mock.patch.object(target, name, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def catalog_request(self, url):
        self.network.append(url)
        self.assertEqual(url, resolve_core.GO_CATALOG)
        return go_catalog()

    def download_source(self, url, path, expected=None):
        self.downloads.append((url, path, expected, path.exists()))
        self.assertEqual(url, "https://codeload.github.com/tailscale/tailscale/tar.gz/" + COMMIT)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.archive)
        digest = hashlib.sha256(self.archive).hexdigest()
        if expected is not None and expected != digest:
            raise ValueError("checksum mismatch")
        return digest

    def resolve(self):
        return resolve_core.resolve(VERSION, COMMIT, self.path, reuse_published=False)

    def write_lock(self, lock):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(lock))


class CoreResolutionTests(CoreLockFixture):
    def test_new_upstream_release_automatically_locks_its_required_go(self):
        lock = self.resolve()
        self.assertEqual(lock["version"], VERSION)
        self.assertEqual(lock["go"]["version"], "1.27.1")
        self.assertEqual(lock["go"]["requirements"], {"go": "1.27.1", "toolchain": None, "upstream": "1.27.1"})
        self.assertEqual(lock["source"]["commit"], COMMIT)
        self.assertEqual(lock["source"]["sha256"], hashlib.sha256(self.archive).hexdigest())
        self.assertEqual(lock["upstream_toolchain_rev"], REVISION)
        self.assertEqual(json.loads(self.path.read_text()), lock)
        self.assertEqual(len(self.network), 1)
        self.no_subprocess.assert_not_called()

    def test_initial_unknown_source_digest_forces_fresh_download(self):
        cached = self.root / ".cache/downloads" / ("tailscale-" + COMMIT + ".tar.gz")
        cached.parent.mkdir(parents=True)
        cached.write_bytes(b"unverified source cache")
        self.resolve()
        self.assertEqual(len(self.downloads), 1)
        self.assertIsNone(self.downloads[0][2])
        self.assertFalse(self.downloads[0][3], "unknown source cache must be removed before fetching")

    def test_cached_lock_reuses_catalog_and_source_digest_without_refetch(self):
        first = self.resolve()
        self.download.reset_mock()
        self.catalog.reset_mock()
        second = self.resolve()
        self.assertEqual(first, second)
        self.download.assert_not_called()
        self.catalog.assert_not_called()
        self.assertEqual(self.source_commit.call_count, 2)

    def test_changed_upstream_tag_rejected_before_network_or_lock_changes(self):
        original = self.resolve()
        self.source_commit.return_value = "e" * 40
        self.download.reset_mock()
        self.catalog.reset_mock()
        with self.assertRaisesRegex(ValueError, "tag changed"):
            self.resolve()
        self.assertEqual(json.loads(self.path.read_text()), original)
        self.download.assert_not_called()
        self.catalog.assert_not_called()

    def test_cached_release_identity_and_recipe_drift_are_rejected(self):
        original = self.resolve()
        cases = (
            ("version", "1.102.5", "different source or release identity"),
            ("build", "r2", "different source or release identity"),
            ("recipe_source_sha256", "f" * 64, "recipe differs"),
        )
        for field, value, message in cases:
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                changed[field] = value
                self.write_lock(changed)
                with self.assertRaisesRegex(ValueError, message):
                    self.resolve()
        changed = copy.deepcopy(original)
        changed["source"]["commit"] = "e" * 40
        changed["source"]["url"] = "https://codeload.github.com/tailscale/tailscale/tar.gz/" + "e" * 40
        self.write_lock(changed)
        with self.assertRaisesRegex(ValueError, "different source or release identity"):
            self.resolve()

    def test_source_version_disagreement_does_not_write_lock(self):
        self.archive = source_archive(version="1.102.5")
        with self.assertRaisesRegex(ValueError, "VERSION.txt disagrees"):
            self.resolve()
        self.assertFalse(self.path.exists())
        self.catalog.assert_not_called()

    def test_signed_published_lock_disagreement_with_cached_lock_is_rejected(self):
        cached = self.resolve()
        published = copy.deepcopy(cached)
        published["go"]["archives"]["arm64"]["sha256"] = "e" * 64
        with mock.patch.object(resolve_core, "published_lock", return_value=published):
            with self.assertRaisesRegex(ValueError, "differs from the signed published lock"):
                resolve_core.resolve(VERSION, COMMIT, self.path, helper=self.root / "helper")
        self.assertEqual(json.loads(self.path.read_text()), cached)


class PublishedCoreLockTests(CoreLockFixture):
    def setUp(self):
        super().setUp()
        self.lock = self.resolve()
        self.metadata = {"schema": 2, "dependency_lock": self.lock}
        self.helper = self.root / "verifier"
        self.helper.write_text("mock verifier")
        self.descriptors = {arch: {"version": VERSION, "build": "r1", "source_commit": COMMIT,
                                   "recipe_sha256": resolve_core.lock_hash(self.lock)} for arch in ("arm", "arm64")}
        self.patch(publish_core, "release_by_tag", return_value={"draft": False})
        self.verify = self.patch(publish_core, "verify_manifest", return_value=self.descriptors)
        self.requests = []
        self.patch(resolve_core, "request", side_effect=self.read_release)

    def read_release(self, url):
        self.requests.append(url)
        base = f"https://github.com/{resolve_core.REPOSITORY}/releases/download/core-v{VERSION}-r1/"
        if url == base + "manifest.json":
            return io.BytesIO(b"signed envelope")
        if url == base + "build-metadata.json":
            return io.BytesIO(json.dumps(self.metadata).encode())
        raise AssertionError("unexpected metadata-directed request: " + url)

    def test_published_lock_is_bound_to_both_verified_architectures(self):
        self.assertEqual(resolve_core.published_lock(VERSION, "r1", self.helper), self.lock)
        self.verify.assert_called_once_with(b"signed envelope", helper=self.helper)
        self.assertEqual(len(self.requests), 2)
        self.no_subprocess.assert_not_called()

    def test_each_signed_architecture_must_match_complete_lock(self):
        for arch in ("arm", "arm64"):
            for field, value in (("version", "1.102.5"), ("build", "r2"),
                                 ("source_commit", "f" * 40), ("recipe_sha256", "f" * 64)):
                with self.subTest(arch=arch, field=field):
                    original = self.descriptors[arch][field]
                    self.descriptors[arch][field] = value
                    with self.assertRaisesRegex(ValueError, "does not match its signed recipe"):
                        resolve_core.published_lock(VERSION, "r1", self.helper)
                    self.descriptors[arch][field] = original

    def test_metadata_dependency_change_invalidates_signed_recipe(self):
        changed = copy.deepcopy(self.lock)
        changed["go"]["archives"]["arm64"]["sha256"] = "e" * 64
        self.metadata["dependency_lock"] = changed
        with self.assertRaisesRegex(ValueError, "does not match its signed recipe"):
            resolve_core.published_lock(VERSION, "r1", self.helper)

    def test_tampered_metadata_cannot_redirect_source_or_go_downloads(self):
        for field in ("source", "go"):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.lock)
                if field == "source":
                    changed["source"]["url"] = "https://untrusted.invalid/source.tar.gz"
                else:
                    changed["go"]["archives"]["amd64"]["url"] = "https://untrusted.invalid/go.tar.gz"
                self.metadata["dependency_lock"] = changed
                with self.assertRaisesRegex(ValueError, "untrusted source|official download URL"):
                    resolve_core.published_lock(VERSION, "r1", self.helper)
        self.assertTrue(all(url.startswith("https://github.com/") for url in self.requests))
        self.no_subprocess.assert_not_called()

    def test_invalid_manifest_stops_reuse_before_any_tool_download(self):
        self.verify.side_effect = ValueError("invalid signature")
        self.download.reset_mock()
        with self.assertRaisesRegex(ValueError, "invalid signature"):
            resolve_core.published_lock(VERSION, "r1", self.helper)
        self.download.assert_not_called()

    def test_legacy_published_metadata_requires_original_build_recipe(self):
        self.metadata = {"schema": 1}
        with self.assertRaisesRegex(ValueError, "predates dependency locks"):
            resolve_core.published_lock(VERSION, "r1", self.helper)


class LockedCoreBuildTests(CoreLockFixture):
    def setUp(self):
        super().setUp()
        self.lock = self.resolve()
        self.downloads.clear()
        self.patch(build_core.platform, "machine", return_value="arm64")
        self.tag_lookup = self.patch(build_core, "source_commit", side_effect=AssertionError("lock must not re-resolve a tag"))
        self.resolution = self.patch(resolve_core, "resolve", side_effect=AssertionError("lock must not resolve dependencies again"))
        self.patch(build_core, "download", side_effect=self.download_input)
        self.extract = self.patch(build_core, "extract")
        self.go_directory = toolchain.toolchain_dir(self.root / ".cache", "core", self.lock["go"], "arm64")
        self.provision = self.patch(toolchain, "provision_go", return_value=self.go_directory)
        self.no_subprocess.side_effect = None

    def download_input(self, url, path, expected):
        self.downloads.append((url, expected))
        if url == self.lock["source"]["url"]:
            self.assertEqual(expected, self.lock["source"]["sha256"])
            self.assertEqual(hashlib.sha256(self.archive).hexdigest(), expected)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.archive)
            return expected
        self.assertEqual(url, "https://github.com/upx/upx/releases/download/v5.0.2/upx-5.0.2-arm64_linux.tar.xz")
        self.assertEqual(expected, self.recipe["upx_sha256"]["arm64"])
        return expected

    def build(self, *extra):
        with mock.patch.object(sys, "argv", ["build_core.py", "--lock", str(self.path), "--arch", "all", *extra]):
            build_core.main()

    def test_build_uses_exact_locked_source_and_go_without_resolving_tag(self):
        self.build()
        self.tag_lookup.assert_not_called()
        self.resolution.assert_not_called()
        self.assertEqual(self.downloads[0], (self.lock["source"]["url"], self.lock["source"]["sha256"]))
        self.assertEqual(self.provision.call_args.args[:4], (self.lock["go"], "arm64", self.root / ".cache", "core"))
        self.assertEqual(self.no_subprocess.call_count, 2)
        commands = [call.args[0] for call in self.no_subprocess.call_args_list]
        self.assertEqual({command[-6] for command in commands}, {"arm", "arm64"})
        expected_go = str(toolchain.toolchain_dir("/cache", "core", self.lock["go"], "arm64") / "go/bin/go")
        self.assertTrue(all(command[-2] == expected_go for command in commands))
        self.assertTrue(all(command[-4] == "tailscale-" + COMMIT for command in commands))
        metadata = json.loads((self.root / "build/core-build.json").read_text())
        self.assertEqual(metadata["dependency_lock"], self.lock)
        self.assertEqual(metadata["recipe_sha256"], resolve_core.lock_hash(self.lock))
        self.assertEqual(metadata["go_version"], "1.27.1")

    def test_source_requirement_or_revision_mismatch_fails_before_compiler(self):
        for changes in ({"go": "1.28.0"}, {"preferred": "1.28.0"}, {"revision": "e" * 40}):
            with self.subTest(changes=changes):
                self.archive = source_archive(**changes)
                self.lock["source"]["sha256"] = hashlib.sha256(self.archive).hexdigest()
                self.write_lock(self.lock)
                with self.assertRaisesRegex(ValueError, "Go requirements differ"):
                    self.build()
        self.provision.assert_not_called()
        self.extract.assert_not_called()
        self.no_subprocess.assert_not_called()
        self.assertFalse((self.root / "build/core-build.json").exists())

    def test_explicit_wrong_version_does_not_download_or_compile(self):
        with self.assertRaisesRegex(ValueError, "requested version differs"):
            self.build("--version", "1.102.5")
        self.assertFalse(self.downloads)
        self.provision.assert_not_called()
        self.no_subprocess.assert_not_called()

    def test_lock_build_number_must_match_recipe_before_download(self):
        changed = copy.deepcopy(self.lock)
        changed["build"] = "r2"
        self.write_lock(changed)
        with self.assertRaisesRegex(ValueError, "locked build differs"):
            self.build()
        self.assertFalse(self.downloads)
        self.provision.assert_not_called()
        self.no_subprocess.assert_not_called()

    def test_malicious_lock_urls_are_rejected_before_download(self):
        changed = copy.deepcopy(self.lock)
        changed["source"]["url"] = "https://untrusted.invalid/archive.tar.gz"
        self.write_lock(changed)
        with self.assertRaisesRegex(ValueError, "untrusted source archive URL"):
            self.build()
        self.assertFalse(self.downloads)
        self.no_subprocess.assert_not_called()


if __name__ == "__main__":
    unittest.main()
