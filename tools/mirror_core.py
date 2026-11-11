#!/usr/bin/env python3
"""Copy the signed stable feed and the archives it names to the download mirror.

The mirror is a fallback for routers that cannot reach GitHub. It is not
trusted: routers verify the feed signature and archive hashes themselves. This
tool still uploads only what the signed feed describes, archives first, so the
mirrored feed never names a file the mirror does not hold.
"""
import datetime
import hashlib
import hmac
import os
import sys
import urllib.error
import urllib.request

from build_core import request
from publish_core import MAX_MANIFEST, REPO, STABLE_URL, verify_manifest

BUCKET = "tailscale-core"
RELEASES = f"https://github.com/{REPO}/releases/download/"
IMMUTABLE = "public, max-age=31536000, immutable"
EMPTY = hashlib.sha256(b"").hexdigest()


def signature(method, host, path, headers, payload_hash, region, key_id, secret, stamp):
    """Return the AWS Signature Version 4 Authorization header value."""
    signed = {name.lower(): " ".join(value.split()) for name, value in headers.items()}
    signed.update({"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": stamp})
    names = ";".join(sorted(signed))
    canonical = "\n".join([method, urllib.request.quote(path, safe="/-_.~"), "",
                           "".join(f"{name}:{signed[name]}\n" for name in sorted(signed)), names, payload_hash])
    scope = f"{stamp[:8]}/{region}/s3/aws4_request"
    text = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = ("AWS4" + secret).encode()
    for part in (stamp[:8], region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    digest = hmac.new(key, text.encode(), hashlib.sha256).hexdigest()
    return f"AWS4-HMAC-SHA256 Credential={key_id}/{scope}, SignedHeaders={names}, Signature={digest}"


class Bucket:
    """The three S3 operations the mirror needs, on one R2 bucket."""

    def __init__(self, account, key_id, secret, token=None):
        self.host = f"{account}.r2.cloudflarestorage.com"
        self.key_id, self.secret, self.token = key_id, secret, token

    def call(self, method, key, body=b"", headers=None):
        headers = dict(headers or {})
        if self.token:
            headers["x-amz-security-token"] = self.token
        path = f"/{BUCKET}/{key}"
        payload_hash = hashlib.sha256(body).hexdigest() if body else EMPTY
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        headers["Authorization"] = signature(method, self.host, path, headers, payload_hash, "auto",
                                             self.key_id, self.secret, stamp)
        headers.update({"x-amz-content-sha256": payload_hash, "x-amz-date": stamp})
        call = urllib.request.Request(f"https://{self.host}{path}", data=body or None, method=method, headers=headers)
        return urllib.request.urlopen(call, timeout=300)

    def size(self, key):
        try:
            with self.call("HEAD", key) as response:
                return int(response.headers["Content-Length"])
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            error.close()
            return None

    def get(self, key):
        try:
            with self.call("GET", key) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            error.close()
            return None

    def put(self, key, data, cache_control, content_type):
        self.call("PUT", key, data, {"Cache-Control": cache_control, "Content-Type": content_type}).close()


def download(url, limit):
    with request(url) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("download exceeds the signed size: " + url)
    return data


def mirror(bucket, fetch=download, verify=verify_manifest):
    """Bring the bucket up to the signed stable feed; return the uploaded keys."""
    feed = fetch(STABLE_URL, MAX_MANIFEST)
    uploaded = []
    for arch, artifact in sorted(verify(feed).items()):
        if not artifact["url"].startswith(RELEASES):
            raise ValueError("signed feed names a foreign download: " + arch)
        key = artifact["url"][len(RELEASES):]
        # Archive names carry version and build, so an object of the signed
        # size is the file this tool verified and uploaded earlier.
        if bucket.size(key) == artifact["size"]:
            continue
        data = fetch(artifact["url"], artifact["size"])
        if len(data) != artifact["size"] or hashlib.sha256(data).hexdigest() != artifact["sha256"]:
            raise ValueError("published archive differs from the signed feed: " + arch)
        bucket.put(key, data, IMMUTABLE, "application/gzip")
        uploaded.append(key)
    key = STABLE_URL[len(RELEASES):]
    if bucket.get(key) != feed:
        # The feed is replaced in place; caches must ask again every time.
        bucket.put(key, feed, "no-cache", "application/json")
        uploaded.append(key)
    return uploaded


def main():
    names = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
    account, key_id, secret = (os.environ.get(name, "") for name in names)
    if not (account and key_id and secret):
        print("::notice::Mirror credentials are not configured; nothing was mirrored.")
        return 0
    uploaded = mirror(Bucket(account, key_id, secret, os.environ.get("R2_SESSION_TOKEN")))
    print("mirror is current" if not uploaded else "uploaded: " + ", ".join(uploaded))
    return 0


if __name__ == "__main__":
    sys.exit(main())
