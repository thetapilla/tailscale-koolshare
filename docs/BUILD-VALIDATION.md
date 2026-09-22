# Build validation — 3.0.0

Validated on 2026-09-22. Official Tailscale **1.102.4**, source commit `bbcd7d1fc2054b9189ebc1531acf74bd880ca0c8`, Go **1.26.6**, UPX **5.0.2** (`--best --lzma`).

Core recipe SHA-256: `0d44a80146beae68a453a114cda96a655c37a624494fe669b33a00c24daf0286`.

| Architecture | Combined core bytes | Packed helper bytes | Core SHA-256 |
|---|---:|---:|---|
| arm | 6,607,132 | 1,867,704 | `6aa43063352be87a716cedcab32c48287df1b8263ee6d83d46668df5486020e0` |
| arm64 | 6,684,044 | 1,830,988 | `a08c40fbccf169f033d33e86557beabca64c3a2eef2e9f10073fd6847d02d0ac` |

Both cores are below the 12 MiB limit. Clean-source rebuilds reproduced both packed core hashes. Per-architecture version/source/recipe/hash records were validated before signing.

Both real CPU architectures passed UPX integrity, CLI and daemon version, packed-helper version, userspace daemon startup, packed-helper LocalAPI `NeedsLogin`, subnet/exit-node preferences and netcheck CLI checks in network-isolated Docker containers.

## Tests

- Original BusyBox 1.25.1 ash: **30 backend**, **24 core/updater**, and **20 installer/uninstaller** tests passed, including post-auth address changes, failed-rule retries, queued NAT-event replay and preservation of existing Fullcone hook ordering.
- Linux Go helper: **11 top-level security tests plus subtests** passed, including real Unix sockets, signatures, archive rejection, deadlines, atomic pointers and log redaction.
- Package and publication tooling: **29 tests** passed, including mixed-version build rejection, immutable release recovery, remote artifact readback, stable-feed downgrade prevention and signature failure paths.
- GitHub workflow YAML parsed successfully. Publication tests use mocked GitHub operations; release publication and readback are separate verification steps.

## Installation archives

| Package | Bytes | SHA-256 |
|---|---:|---|
| `tailscale_3.0.0_universal.tar.gz` | 17,056,215 | `137c787a4bd06501d10fa50c19989ee99e223ee20d389edf70cad1f0749ff606` |
| `tailscale_3.0.0_hnd.tar.gz` | 8,537,371 | `35a1a57874ebd75ddf60583c099689d60c14ff52d6666e84ebee49ed3fdc3999` |
| `tailscale_3.0.0_qca.tar.gz` | 8,537,375 | `f8cae53eb2b4add88e13893304bb3025c0a395b0ad4cf571a9ceed35dba913d5` |
| `tailscale_3.0.0_ipq32.tar.gz` | 8,537,373 | `84318af4d6fbd84b54e4c15fe86c084e1dc0cb0e0cfb7c282cb9524e2a8c5683` |
| `tailscale_3.0.0_ipq64.tar.gz` | 8,579,195 | `1599ba025c0230bea8d0e84b022fa847c78fe8a2b7264a20d77170791f26ddba` |
| `tailscale_3.0.0_mtk.tar.gz` | 8,579,195 | `caf8939f3998cac220cbd70213d1949e7e2900155f04525d4d435307f72411f0` |

Every archived regular file matches `manifest.sha256`; signed bundled core descriptors were reverified against the committed public key. All package modes, ownership, timestamps, platform markers, ELF architecture and core size limits were checked. Universal-selected files are byte-identical to their corresponding single-platform package files.

A second full packaging run reproduced all six archive hashes. Recreating the six archives in the pinned Linux Python 3.12 container (zlib 1.3.1) also matched the host-generated archives (zlib 1.2.12) byte-for-byte.

Artifacts are in `../dist/` with `SHA256SUMS`; the machine-readable verification record is `build/package-validation.json`. Real router TUN/firewall operation, firmware ABI compatibility, login, subnet routing and exit-node traffic require hardware acceptance testing.
