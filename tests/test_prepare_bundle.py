"""Check bundle import boundaries; helper cryptography is covered by Go tests."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from prepare_bundle import prepare


class PrepareBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "tools").mkdir()
        (self.root / "tools/bundled_core.json").write_text('{"version":"1.104.1","build":"r1"}')
        self.assets = self.root / "release"
        self.assets.mkdir()
        (self.assets / "manifest.json").write_text("signed envelope")
        self.descriptors = {}
        for arch in ("arm", "arm64"):
            name = arch + ".tar.gz"
            (self.assets / name).write_bytes(arch.encode())
            self.descriptors[arch] = dict(version="1.104.1", build="r1", arch=arch,
                                          source_commit="a" * 40, recipe_sha256="b" * 64,
                                          url="https://example.invalid/" + name, sha256="c" * 64)
        for target in ("cores/arm", "cores/arm64", "core-release"):
            directory = self.root / "build" / target
            directory.mkdir(parents=True)
            (directory / "previous").write_text(target)
        self.before = self.inputs()

    def inputs(self):
        return {str(p.relative_to(self.root / "build")): p.read_bytes()
                for p in (self.root / "build").rglob("*") if p.is_file()}

    def verify(self, command):
        return json.dumps(self.descriptors[command[-1]]).encode()

    def extract(self, command, **kwargs):
        destination = Path(command[-1])
        destination.mkdir()
        (destination / "descriptor.json").write_bytes(Path(command[-2]).read_bytes())
        (destination / "tailscale.combined").write_bytes(Path(command[-3]).read_bytes())

    def run_import(self, verify=None, extract=None):
        with patch("prepare_bundle.subprocess.check_output", side_effect=verify or self.verify), \
             patch("prepare_bundle.subprocess.run", side_effect=extract or self.extract):
            return prepare(self.root, self.root / "helper", self.assets)

    def test_import_keeps_signed_envelope_and_replaces_previous_build_inputs(self):
        self.assertEqual(self.run_import(), {"version": "1.104.1", "build": "r1"})
        self.assertEqual((self.root / "build/core-release/manifest.json").read_bytes(),
                         (self.assets / "manifest.json").read_bytes())
        self.assertFalse(any(name.endswith("previous") for name in self.inputs()))
        for arch in ("arm", "arm64"):
            self.assertEqual((self.root / "build/cores" / arch / "tailscale.combined").read_bytes(), arch.encode())

    def test_signature_failure_preserves_all_previous_inputs(self):
        def reject(command):
            raise subprocess.CalledProcessError(1, command)
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_import(verify=reject)
        self.assertEqual(self.inputs(), self.before)

    def test_signed_but_unpinned_version_preserves_all_previous_inputs(self):
        self.descriptors["arm64"]["version"] = "1.106.0"
        with self.assertRaisesRegex(ValueError, "bundled core pin"):
            self.run_import()
        self.assertEqual(self.inputs(), self.before)

    def test_second_architecture_extraction_failure_preserves_all_previous_inputs(self):
        def extract(command, **kwargs):
            if command[-1].endswith("arm64"):
                raise subprocess.CalledProcessError(1, command)
            self.extract(command, **kwargs)
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_import(extract=extract)
        self.assertEqual(self.inputs(), self.before)

    def test_mixed_source_provenance_preserves_all_previous_inputs(self):
        self.descriptors["arm64"]["source_commit"] = "d" * 40
        with self.assertRaisesRegex(ValueError, "different signed provenance"):
            self.run_import()
        self.assertEqual(self.inputs(), self.before)
