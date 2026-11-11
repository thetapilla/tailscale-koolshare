import hashlib
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import mirror_core


class FakeBucket:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.puts = []

    def size(self, key):
        return len(self.objects[key]) if key in self.objects else None

    def get(self, key):
        return self.objects.get(key)

    def put(self, key, data, cache_control, content_type):
        self.objects[key] = data
        self.puts.append((key, cache_control, content_type))


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.feed = b'{"payload":"signed","signature":"feed"}'
        self.archives = {arch: (arch + "-archive").encode() * 64 for arch in ("arm", "arm64")}
        self.keys = {arch: f"core-v1.104.6-r2/tailscale-core_1.104.6_r2_{arch}.tar.gz" for arch in self.archives}
        self.descriptors = {arch: {"url": mirror_core.RELEASES + self.keys[arch], "size": len(data),
                                   "sha256": hashlib.sha256(data).hexdigest()}
                            for arch, data in self.archives.items()}
        self.fetched = []

    def fetch(self, url, limit):
        self.fetched.append(url)
        if url == mirror_core.STABLE_URL:
            return self.feed
        return next(data for arch, data in self.archives.items() if url.endswith(self.keys[arch]))

    def run_mirror(self, bucket):
        return mirror_core.mirror(bucket, fetch=self.fetch, verify=lambda feed: self.descriptors)

    def test_archives_are_uploaded_before_the_feed_that_names_them(self):
        bucket = FakeBucket()
        uploaded = self.run_mirror(bucket)
        self.assertEqual(uploaded, [self.keys["arm"], self.keys["arm64"], "core-stable/manifest.json"])
        self.assertEqual(bucket.puts, [(self.keys["arm"], mirror_core.IMMUTABLE, "application/gzip"),
                                       (self.keys["arm64"], mirror_core.IMMUTABLE, "application/gzip"),
                                       ("core-stable/manifest.json", "no-cache", "application/json")])
        self.assertEqual(bucket.objects["core-stable/manifest.json"], self.feed)
        self.assertEqual(bucket.objects[self.keys["arm64"]], self.archives["arm64"])

    def test_current_mirror_is_left_untouched_and_downloads_nothing_again(self):
        bucket = FakeBucket()
        self.run_mirror(bucket)
        bucket.puts.clear()
        self.fetched.clear()
        self.assertEqual(self.run_mirror(bucket), [])
        self.assertEqual(bucket.puts, [])
        self.assertEqual(self.fetched, [mirror_core.STABLE_URL])

    def test_missing_archive_and_stale_feed_are_repaired(self):
        bucket = FakeBucket()
        self.run_mirror(bucket)
        del bucket.objects[self.keys["arm"]]
        bucket.objects["core-stable/manifest.json"] = b"older feed"
        bucket.puts.clear()
        self.assertEqual(self.run_mirror(bucket), [self.keys["arm"], "core-stable/manifest.json"])

    def test_archive_that_differs_from_the_signed_feed_stops_before_any_feed_upload(self):
        for change in ("content", "size"):
            with self.subTest(change=change):
                bucket = FakeBucket()
                good = self.archives["arm64"]
                self.archives["arm64"] = good[:-1] + b"!" if change == "content" else good + b"!"
                with self.assertRaisesRegex(ValueError, "differs from the signed feed"):
                    self.run_mirror(bucket)
                self.archives["arm64"] = good
                self.assertEqual([key for key, _, _ in bucket.puts], [self.keys["arm"]])
                self.assertNotIn("core-stable/manifest.json", bucket.objects)

    def test_signed_feed_cannot_send_the_mirror_elsewhere(self):
        self.descriptors["arm"]["url"] = "https://example.test/core.tar.gz"
        bucket = FakeBucket()
        with self.assertRaisesRegex(ValueError, "foreign download"):
            self.run_mirror(bucket)
        self.assertEqual(bucket.puts, [])

    def test_unverified_feed_is_never_mirrored(self):
        bucket = FakeBucket()

        def reject(feed):
            raise ValueError("bad signature")

        with self.assertRaisesRegex(ValueError, "bad signature"):
            mirror_core.mirror(bucket, fetch=self.fetch, verify=reject)
        self.assertEqual(bucket.puts, [])

    def test_missing_credentials_skip_without_failing_the_release(self):
        output = io.StringIO()
        with mock.patch.dict("os.environ", {"R2_ACCOUNT_ID": "account", "R2_ACCESS_KEY_ID": "", "R2_SECRET_ACCESS_KEY": ""}), \
                mock.patch.object(mirror_core, "mirror", side_effect=AssertionError("mirrored")), \
                mock.patch("sys.stdout", output):
            self.assertEqual(mirror_core.main(), 0)
        self.assertIn("not configured", output.getvalue())

    def test_request_signature_matches_the_published_reference_value(self):
        # "GET Object" example from the Amazon S3 Signature Version 4 documentation.
        value = mirror_core.signature("GET", "examplebucket.s3.amazonaws.com", "/test.txt", {"Range": "bytes=0-9"},
                                      mirror_core.EMPTY, "us-east-1", "AKIAIOSFODNN7EXAMPLE",
                                      "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "20130524T000000Z")
        self.assertEqual(value, "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
                                "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
                                "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")


if __name__ == "__main__":
    unittest.main()
