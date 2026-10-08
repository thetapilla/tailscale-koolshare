#!/usr/bin/env python3
"""Helper builds select and provision Go independently of the Tailscale core."""
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
import build_helper
import test_helper
from toolchain import resolve_go, toolchain_dir


def archive(version):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        for name, data, mode in (("go/bin/go", b"#!/bin/sh\nexit 0\n", 0o755),
                                 ("go/VERSION", ("go" + version + "\n").encode(), 0o644)):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(stream.getvalue(), mtime=0)


def catalog(*versions):
    releases = []
    for version in versions:
        data = archive(version)
        releases.append({"version": "go" + version, "stable": True, "files": [
            {"version": "go" + version, "filename": f"go{version}.linux-{arch}.tar.gz",
             "os": "linux", "arch": arch, "kind": "archive", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
            for arch in ("amd64", "arm64")
        ]})
    return releases


class HelperToolchainTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "tools").mkdir()
        (self.root / "go.mod").write_text("module example.invalid/helper\n\ngo 1.24.0\n")
        self.recipe = build_helper.helper_recipe()
        (self.root / "tools/helper_recipe.json").write_text(json.dumps(self.recipe))
        self.catalog = catalog("1.26.6", "1.27.1", "1.28.0")

    def test_fresh_checkout_chooses_own_baseline_without_core_recipe_or_cache(self):
        with mock.patch.object(build_helper, "json_url", return_value=self.catalog) as fetch:
            lock = build_helper.prepare_helper_toolchain(self.root)
        self.assertEqual(lock["version"], "1.26.6")
        fetch.assert_called_once_with(build_helper.GO_CATALOG)
        self.assertTrue((self.root / "build/helper-toolchain.json").is_file())
        self.assertFalse((self.root / ".cache").exists())
        self.assertFalse((self.root / "tools/core_recipe.json").exists())

    def test_valid_cached_lock_needs_no_network(self):
        first = build_helper.prepare_helper_toolchain(self.root, self.catalog)
        with mock.patch.object(build_helper, "json_url", side_effect=AssertionError("unexpected network")):
            second = build_helper.prepare_helper_toolchain(self.root)
        self.assertEqual(first, second)

    def test_own_go_mod_increase_selects_new_official_toolchain(self):
        first = build_helper.prepare_helper_toolchain(self.root, self.catalog)
        (self.root / "go.mod").write_text("module example.invalid/helper\ngo 1.27.1\n")
        second = build_helper.prepare_helper_toolchain(self.root, self.catalog)
        self.assertEqual(second["version"], "1.27.1")
        self.assertNotEqual(build_helper.cache_key(first), build_helper.cache_key(second))
        self.assertEqual(second["requirements"]["upstream"], "1.26.6")

    def test_own_toolchain_directive_above_language_requirement_is_honored(self):
        (self.root / "go.mod").write_text("module example.invalid/helper\ngo 1.27.1\ntoolchain go1.28.0\n")
        selected = build_helper.prepare_helper_toolchain(self.root, self.catalog)
        self.assertEqual(selected["version"], "1.28.0")

    def test_recipe_baseline_increase_refreshes_lock(self):
        build_helper.prepare_helper_toolchain(self.root, self.catalog)
        self.recipe["go_baseline"] = "1.27.1"
        (self.root / "tools/helper_recipe.json").write_text(json.dumps(self.recipe))
        self.assertEqual(build_helper.prepare_helper_toolchain(self.root, self.catalog)["version"], "1.27.1")

    def test_saved_core_lock_cannot_replace_helper_selection(self):
        build_helper.prepare_helper_toolchain(self.root, self.catalog)
        path = self.root / "build/helper-toolchain.json"
        document = json.loads(path.read_text())
        document["go"] = resolve_go("go 1.27.1\n", "1.27.1", self.catalog)
        path.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "required Go version|own Go requirements"):
            build_helper.prepare_helper_toolchain(self.root, self.catalog)

    def test_source_mismatch_rejected_inside_container(self):
        build_helper.prepare_helper_toolchain(self.root, self.catalog)
        document = json.loads((self.root / "build/helper-toolchain.json").read_text())
        (self.root / "go.mod").write_text("module example.invalid/helper\ngo 1.27.1\n")
        with self.assertRaisesRegex(ValueError, "differs from its source"):
            build_helper.validate_helper_lock(document, self.root)

    def test_fresh_provision_verifies_archive_and_does_not_use_core_cache(self):
        lock = build_helper.prepare_helper_toolchain(self.root, self.catalog)
        wrong = self.root / ".cache/toolchains/arm64/go/bin"
        wrong.mkdir(parents=True)
        (wrong / "go").write_text("stale compiler")
        downloaded = []
        def download(url, destination, expected):
            downloaded.append(url)
            data = archive(lock["version"])
            self.assertEqual(hashlib.sha256(data).hexdigest(), expected)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        with mock.patch.object(build_helper, "download", side_effect=download):
            prepared = build_helper.provision_helper_go(self.root, "arm64", lock)
            cached = build_helper.provision_helper_go(self.root, "arm64", lock)
        self.assertEqual(prepared, toolchain_dir(self.root / ".cache", "helper", lock, "arm64"))
        self.assertEqual(prepared, cached)
        self.assertEqual((prepared / "go/VERSION").read_text(), "go1.26.6\n")
        self.assertEqual(len(downloaded), 1)
        self.assertEqual((wrong / "go").read_text(), "stale compiler")

    def test_checksum_mismatch_prevents_fresh_provision(self):
        lock = build_helper.prepare_helper_toolchain(self.root, self.catalog)
        def download(url, destination, expected):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"corrupt archive")
        with mock.patch.object(build_helper, "download", side_effect=download):
            with self.assertRaisesRegex(ValueError, "locked size or SHA-256"):
                build_helper.provision_helper_go(self.root, "arm64", lock)
        self.assertFalse(toolchain_dir(self.root / ".cache", "helper", lock, "arm64").exists())

    def test_test_runner_prepares_and_provisions_own_go_before_docker(self):
        lock = build_helper.prepare_helper_toolchain(self.root, self.catalog)
        calls = []
        with mock.patch.object(test_helper, "ROOT", self.root), \
             mock.patch.object(test_helper, "host_arch", return_value="arm64"), \
             mock.patch.object(test_helper, "prepare_helper_toolchain", side_effect=lambda root: calls.append("select") or lock), \
             mock.patch.object(test_helper, "provision_helper_go", side_effect=lambda *args: calls.append("provision")), \
             mock.patch.object(test_helper.subprocess, "run", side_effect=lambda command, **kwargs: calls.append(command)):
            test_helper.main()
        self.assertEqual(calls[:2], ["select", "provision"])
        command = calls[2]
        go = str(toolchain_dir("/cache", "helper", lock, "arm64") / "go/bin/go")
        self.assertEqual(command[-4:], [go, "test", "-v", "./cmd/tsks-helper"])
        self.assertIn("--network=none", command)
        self.assertIn("GOTOOLCHAIN=local", command)


if __name__ == "__main__":
    unittest.main()
