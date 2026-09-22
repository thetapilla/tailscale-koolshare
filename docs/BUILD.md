# Build and release

Requires Python 3.12+ and Docker. The build downloads official sources into the ignored `.cache/` directory, verifies the initial release tag against its pinned source commit, and verifies the Go and UPX archives against official published SHA-256 checksums. Later stable versions resolve the official upstream tag to a commit and record the downloaded source archive checksum. Go modules remain checked against upstream `go.sum` and the Go checksum database.

`tools/core_recipe.json` pins Go 1.26.6, UPX 5.0.2, the container digest, the thirteen feature tags, initial Tailscale v1.102.4 source commit, and tool archive digests for Linux amd64/arm64 build hosts. The recipe hash covers that JSON and both core build scripts. Compiler settings are `CGO_ENABLED=0`, `GOOS=linux`, `GOARM=7` for ARM32 and `GOARM64=v8.0` for ARM64. Both targets use `-trimpath`, `-buildvcs=false`, `-mod=readonly`, stripped symbols, an empty build ID, and explicit upstream version/commit stamps. UPX uses `--best --lzma`; the compressed combined executable must be at most 12 MiB and pass `upx --test`.

Run from the repository root:

```sh
python3 tools/build_core.py
python3 tools/build_helper.py
python3 tools/smoke_core.py
python3 tools/test_helper.py
python3 -m unittest discover -s tests -p 'test_*.py' -v
node tests/test_ui.js
python3 tools/test_busybox.py
python3 tools/release_core.py --key .secrets/release.key
python3 tools/package.py
```

Docker mounts only the required tool scripts and helper source files read-only; the signing-key directory is never mounted. Only this project's `.cache/` and `build/` mounts are writable, and core/helper compilation runs with the invoking user's UID/GID so Linux host outputs stay writable. Smoke tests use disposable, read-only containers without network access; they exercise both architectures through native execution or QEMU. They check CLI/daemon and packed-helper versions, userspace daemon startup, actual packed-helper LocalAPI `NeedsLogin` status, route and exit-node preferences, and netcheck help. Full TUN, firewall, router ABI and real tailnet connectivity are covered by router acceptance testing, not this isolated smoke test.

The Ed25519 signing key is a 64-byte key encoded as 128 hexadecimal characters, stored locally as `.secrets/release.key`. `plugin/release.pub` is the matching public key. Generate a new key only when initially provisioning a new trust root; do not replace it when rebuilding an existing installation. `release_core.py` signs with the host helper, immediately verifies both architecture descriptors, and never publishes. Use `--created-at` with the prior timestamp to reproduce an identical signed envelope. Each architecture's successful build record binds its version, source commit, recipe hash, size and binary hash; both records must match before signing, so incomplete or mixed-version builds fail closed. Core archives themselves use fixed metadata and timestamps and contain exactly one executable, `tailscale.combined`.

The six installation archives are written to `../dist/`: universal, hnd, qca, ipq32, ipq64 and mtk, plus `SHA256SUMS`. Every archive has one `tailscale/` root. The universal package contains both architectures and five `.valid` lines; HND/QCA/IPQ32 use ARM32 and IPQ64/MTK use ARM64. The same script, resource and architecture payload bytes are used in universal and platform packages. Packaging updates `version` before archiving, validates the signed descriptors and ELF machine type, writes `manifest.sha256`, and normalizes all modes, ownership, ordering and timestamps. Per-platform staging lives under `build/packages/` and leaves the source payload untouched. The installer creates the relative CLI/daemon symlinks and installs only the selected architecture.

## GitHub Actions

`core-release.yml` reads the official `https://pkgs.tailscale.com/stable/?mode=json` feed's `TarballsVersion` and verifies the matching official GitHub source tag daily at 03:23 UTC and also accepts a stable version through `workflow_dispatch`. The check job compiles the verifier and skips only when the signed stable feed already identifies the requested version/build or a newer release. An existing immutable release with a missing or outdated feed proceeds through verification and recovery. Builds and smoke tests have read-only repository access and no signing secret. Only the publish job has `contents: write`, and signing uses the `TSKS_SIGNING_KEY` secret in the `core-release` environment. The secret must match committed `plugin/release.pub`; verification fails before publication otherwise.

Before publication, the job verifies both signed descriptors, archive sizes and SHA-256 hashes, and safe extraction of their single ELF executable. It then creates the versioned release as a draft, uploads both cores, checksums, metadata and the signed envelope, and makes it public. Both archives are downloaded again and their size and SHA-256 checked against the signed manifest before the stable feed can advance. The script never replaces immutable assets. Existing public releases resume only after their remote signed envelope matches both local descriptors and both remote archive hashes pass; feed recovery reuses the original remote envelope. A complete draft is verified before publication, while an incomplete draft fails for explicit cleanup. The mutable `core-stable/manifest.json` asset advances only after the immutable cores are available and verified; matching feeds are left untouched and downgrade attempts fail.

Recipe changes require a reviewed increment of `build` (for example `r1` to `r2`) before publishing an already released upstream version. A future upstream version requiring a newer Go toolchain must fail closed until the pinned recipe is reviewed and updated. Action implementations are pinned to verified official commit SHAs. No release is pushed or published by the local build commands.

The legacy shell gate builds original BusyBox 1.25.1 from its official HTTPS source archive, pinned to SHA-256 `27667e0f2328fdbd79cfd622e4453e5c57e58f781c5da97c9be337d93aa2a02e`. The fixture enables standard ash arithmetic and built-ins, then runs backend, updater and installer tests in a separate network-isolated container. All filesystem mutations are temporary test roots. The fixture compiler packages are installed only inside its disposable Docker build container.
