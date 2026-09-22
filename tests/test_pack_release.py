import copy
import hashlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import check_stable
import publish_core


def not_found():
    return urllib.error.HTTPError("https://api.github.com/", 404, "not found", {}, None)


class ReleasePublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "build/core-release"
        self.out.mkdir(parents=True)
        (self.out / "manifest.json").write_bytes(b"local envelope")
        self.tag = "core-v1.102.4-r1"
        self.archives = {arch: (arch + " archive").encode() for arch in publish_core.ARCHES}
        self.descriptors = {
            arch: {"version": "1.102.4", "build": "r1", "arch": arch, "source_commit": "a" * 40,
                   "recipe_sha256": "b" * 64, "unpacked_size": 1234, "binary_sha256": "c" * 64,
                   "url": f"https://github.com/{publish_core.REPO}/releases/download/{self.tag}/tailscale-core_1.102.4_r1_{arch}.tar.gz",
                   "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for arch, data in self.archives.items()}
        self.envelopes = {name: copy.deepcopy(self.descriptors)
                          for name in (b"local envelope", b"remote envelope", b"stable envelope")}
        self.releases = {}
        self.stable_bytes = None
        self.remote_bytes = b"remote envelope"
        self.fail_upload = False
        self.uploads = []
        self.events = []
        self.validation = mock.patch.object(publish_core, "validate_archives", return_value=self.descriptors)
        self.validation.start()
        self.addCleanup(self.validation.stop)
        patches = {
            "ROOT": mock.patch.object(publish_core, "ROOT", self.root),
            "verified": mock.patch.object(publish_core, "verified", side_effect=self.verify),
            "json_url": mock.patch.object(publish_core, "json_url", side_effect=self.api),
            "request": mock.patch.object(publish_core, "request", side_effect=self.download),
            "gh": mock.patch.object(publish_core, "gh", side_effect=self.github),
            "env": mock.patch.dict("os.environ", {"GITHUB_SHA": "a" * 40}),
        }
        for name, patch in patches.items():
            setattr(self, name + "_mock", patch.start())
            self.addCleanup(patch.stop)

    def verify(self, path, arch="arm", helper=None):
        data = Path(path).read_bytes()
        self.events.append(("verify", data, arch))
        if data not in self.envelopes:
            raise ValueError("invalid signature")
        return copy.deepcopy(self.envelopes[data][arch])

    def api(self, url):
        tag = url.rsplit("/", 1)[1]
        if tag not in self.releases:
            raise not_found()
        return self.releases[tag]

    def download(self, url):
        self.events.append(("download", url))
        if url == publish_core.STABLE_URL:
            if self.stable_bytes is None:
                raise not_found()
            return io.BytesIO(self.stable_bytes)
        if url.endswith("/manifest.json"):
            return io.BytesIO(self.remote_bytes)
        for arch, desc in self.descriptors.items():
            if url == desc["url"]:
                return io.BytesIO(self.archives[arch])
        raise AssertionError("unexpected network request: " + url)

    def github(self, *args):
        self.events.append(("gh", *args))
        action, tag = args[1:3]
        if action == "create":
            self.releases[tag] = {"draft": "--draft" in args}
        elif action == "edit":
            self.releases[tag]["draft"] = False
        elif action == "download":
            directory = Path(args[args.index("--dir") + 1])
            (directory / "manifest.json").write_bytes(self.remote_bytes)
            for arch, desc in self.descriptors.items():
                (directory / desc["url"].rsplit("/", 1)[1]).write_bytes(self.archives[arch])
        elif action == "upload":
            if self.fail_upload:
                raise subprocess.CalledProcessError(1, ["gh", *args])
            body = Path(args[3]).read_bytes()
            self.uploads.append((tag, body))
            self.stable_bytes = body

    def complete_draft(self):
        names = {"manifest.json", "SHA256SUMS", "build-metadata.json"}
        names.update(desc["url"].rsplit("/", 1)[1] for desc in self.descriptors.values())
        return {"draft": True, "assets": [{"name": name, "state": "uploaded"} for name in names]}

    def test_assets_are_public_and_read_back_before_feed_changes(self):
        publish_core.main()
        calls = [call.args for call in self.gh_mock.call_args_list]
        self.assertEqual([call[1] for call in calls], ["create", "edit", "create", "upload"])
        self.assertEqual(calls[0][:3], ("release", "create", self.tag))
        self.assertIn("--draft", calls[0])
        self.assertEqual(calls[1], ("release", "edit", self.tag, "--draft=false"))
        self.assertEqual(calls[2][:3], ("release", "create", "core-stable"))
        self.assertEqual(calls[3][:3], ("release", "upload", "core-stable"))
        self.assertTrue(all("--clobber" not in call for call in calls[:3]))
        upload_index = next(i for i, event in enumerate(self.events) if event[:3] == ("gh", "release", "upload"))
        for desc in self.descriptors.values():
            self.assertLess(self.events.index(("download", desc["url"])), upload_index)
        self.assertEqual(self.uploads, [("core-stable", b"local envelope")])

    def test_existing_release_resumes_with_remote_envelope_without_immutable_writes(self):
        self.releases[self.tag] = {}
        publish_core.main()
        self.assertEqual(self.uploads, [("core-stable", b"remote envelope")])
        self.assertEqual([call.args[:3] for call in self.gh_mock.call_args_list],
                         [("release", "create", "core-stable"), ("release", "upload", "core-stable")])
        for arch, desc in self.descriptors.items():
            self.assertIn(("verify", b"remote envelope", arch), self.events)
            self.assertIn(("download", desc["url"]), self.events)

    def test_resume_after_stable_upload_failure_repairs_empty_feed_release(self):
        self.fail_upload = True
        with self.assertRaises(subprocess.CalledProcessError):
            publish_core.main()
        self.assertIn(self.tag, self.releases)
        self.assertIn("core-stable", self.releases)
        self.fail_upload = False
        self.gh_mock.reset_mock()
        publish_core.main()
        self.assertEqual(self.gh_mock.call_count, 1)
        self.assertEqual(self.gh_mock.call_args.args[:3], ("release", "upload", "core-stable"))
        self.assertEqual(self.uploads, [("core-stable", b"remote envelope")])

    def test_matching_stable_feed_is_idempotent(self):
        self.releases.update({self.tag: {}, "core-stable": {}})
        self.stable_bytes = b"stable envelope"
        publish_core.main()
        self.gh_mock.assert_not_called()
        for desc in self.descriptors.values():
            self.assertIn(("download", desc["url"]), self.events)

    def test_stable_feed_never_moves_backwards(self):
        self.releases["core-stable"] = {}
        self.stable_bytes = b"stable envelope"
        for desc in self.envelopes[self.stable_bytes].values():
            desc["version"] = "1.104.0"
        with self.assertRaisesRegex(RuntimeError, "downgrade"):
            publish_core.main()
        self.gh_mock.assert_not_called()

    def test_same_identity_conflicting_stable_payload_is_rejected(self):
        self.releases.update({self.tag: {}, "core-stable": {}})
        self.stable_bytes = b"stable envelope"
        self.envelopes[self.stable_bytes]["arm64"]["sha256"] = "d" * 64
        with self.assertRaisesRegex(ValueError, "differs"):
            publish_core.main()
        self.gh_mock.assert_not_called()

    def test_existing_immutable_provenance_and_hash_mismatches_are_rejected(self):
        self.releases[self.tag] = {}
        changes = {"version": "1.104.0", "build": "r2", "arch": "arm", "source_commit": "e" * 40,
                   "recipe_sha256": "e" * 64, "sha256": "e" * 64, "binary_sha256": "e" * 64,
                   "size": 10, "unpacked_size": 10, "url": "https://example.test/other"}
        for field, value in changes.items():
            with self.subTest(field=field):
                self.envelopes[self.remote_bytes] = copy.deepcopy(self.descriptors)
                self.envelopes[self.remote_bytes]["arm64"][field] = value
                with self.assertRaisesRegex(ValueError, "differs"):
                    publish_core.main()
                self.gh_mock.assert_not_called()

    def test_unverified_remote_manifest_is_rejected(self):
        self.releases[self.tag] = {}
        self.remote_bytes = b"unsigned payload"
        with self.assertRaisesRegex(ValueError, "invalid signature"):
            publish_core.main()
        self.gh_mock.assert_not_called()

    def test_unverified_stable_feed_is_not_overwritten(self):
        self.releases["core-stable"] = {}
        self.stable_bytes = b"unsigned payload"
        with self.assertRaisesRegex(ValueError, "invalid signature"):
            publish_core.main()
        self.gh_mock.assert_not_called()

    def test_failed_remote_hash_readback_never_advances_feed(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                self.releases = {self.tag: {}} if existing else {}
                self.gh_mock.reset_mock()
                self.archives["arm64"] = b"bad archive"
                with self.assertRaisesRegex(ValueError, "published archive"):
                    publish_core.main()
                self.assertFalse(any("core-stable" in call.args for call in self.gh_mock.call_args_list))

    def test_complete_draft_is_verified_before_undrafting(self):
        self.releases[self.tag] = self.complete_draft()
        publish_core.main()
        calls = [call.args for call in self.gh_mock.call_args_list]
        self.assertEqual([call[1] for call in calls], ["download", "edit", "create", "upload"])
        undraft_index = next(i for i, event in enumerate(self.events) if event[:3] == ("gh", "release", "edit"))
        for arch in publish_core.ARCHES:
            self.assertLess(self.events.index(("verify", b"remote envelope", arch)), undraft_index)
        self.assertEqual(self.uploads, [("core-stable", b"remote envelope")])

    def test_incomplete_draft_requires_cleanup_without_mutation(self):
        self.releases[self.tag] = {"draft": True, "assets": []}
        with self.assertRaisesRegex(RuntimeError, "manually remove"):
            publish_core.main()
        self.gh_mock.assert_not_called()

    def test_corrupt_complete_draft_is_not_published(self):
        self.releases[self.tag] = self.complete_draft()
        self.archives["arm64"] = b"bad archive"
        with self.assertRaisesRegex(ValueError, "draft archive"):
            publish_core.main()
        self.assertEqual([call.args[1] for call in self.gh_mock.call_args_list], ["download"])

    def test_local_archive_hash_checked_before_publish(self):
        archive = self.out / self.descriptors["arm"]["url"].rsplit("/", 1)[1]
        archive.write_bytes(b"changed")
        self.validation.stop()
        with self.assertRaisesRegex(ValueError, "archive size/hash differs"):
            publish_core.main()
        self.gh_mock.assert_not_called()

    def test_manifest_size_limit_checked_before_verification(self):
        with self.assertRaisesRegex(ValueError, "maximum size"):
            publish_core.verify_manifest(b"x" * 65537)
        self.verified_mock.assert_not_called()


class StableCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.helper = Path(self.temp.name) / "helper"
        self.helper.write_text("fixture")

    def test_missing_verifier_never_skips_build(self):
        with mock.patch.object(check_stable, "remote_manifest") as remote:
            self.assertTrue(check_stable.update_needed("1.102.4", self.helper.with_name("missing")))
            remote.assert_not_called()

    def test_skip_only_when_verified_feed_is_same_or_newer(self):
        for version, build, needed in (("1.100.0", "r9", True), ("1.102.4", "r1", False),
                                       ("1.102.4", "r2", False), ("1.104.0", "r1", False)):
            with self.subTest(version=version, build=build), \
                 mock.patch.object(check_stable, "remote_manifest", return_value=(b"signed", {"arm": {"version": version, "build": build}})) as remote:
                self.assertEqual(check_stable.update_needed("1.102.4", self.helper), needed)
                remote.assert_called_once_with(publish_core.STABLE_URL, missing_ok=True, helper=self.helper)

    def test_missing_or_invalid_signed_feed_requests_build(self):
        with mock.patch.object(check_stable, "remote_manifest", return_value=(None, None)):
            self.assertTrue(check_stable.update_needed("1.102.4", self.helper))
        for error in (ValueError("bad signature"), subprocess.CalledProcessError(1, "verify"), not_found()):
            with self.subTest(error=type(error)), mock.patch.object(check_stable, "remote_manifest", side_effect=error):
                self.assertTrue(check_stable.update_needed("1.102.4", self.helper))


if __name__ == "__main__":
    unittest.main()
