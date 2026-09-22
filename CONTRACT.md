# Runtime and packaging interfaces — 3.0.0

Repository root contains plugin/, cmd/tsks-helper/, tools/, tests/ and docs/.
Runtime scripts target router BusyBox ash 1.25. Tests use isolated filesystem roots and mocked router commands.

## Paths

- KSROOT=${TSKS_ROOT:-/koolshare}; test override must be explicitly set by harness.
- DATA=$KSROOT/tailscale; DATA/cores/<version>-<build>-<arch>/; DATA/current and DATA/previous are relative symlinks to cores/... .
- STATE=$KSROOT/configs/tailscale/tailscaled.state (preserve existing identity).
- RUN=${TSKS_RUN:-/tmp/tailscale3}; socket=$RUN/tailscaled.sock; lifecycle lock=$RUN/operation.lock (flock).
- Public job JSON/text only: ${TSKS_WEB:-/tmp/upload}/tailscale3_<numeric-job-id>.json/.log; browser /_temp/ URLs. No secrets/state snapshots in this directory.
- Helper=$KSROOT/bin/tsks-helper; public verification key=$DATA/release.pub; actual CLI/daemon $DATA/current/tailscale and tailscaled symlinks to tailscale.combined.
- Frontend /koolshare/webs/Module_tailscale.asp; icon preserved.
- Core feed https://github.com/thetapilla/tailscale-koolshare/releases/download/core-stable/manifest.json .

## Components

`tailscale_lib.sh` provides lifecycle, locking, job, firewall and watchdog primitives. The core updater shares this lock and uses a persistent transaction journal. The Go helper implements bounded local operations and cryptographic verification. The native page uses the software center transport and structured task results. Build tools generate the two architecture payloads and six installation archives.

## Host/API

httpdb invokes method with request id as $1 and params following ($2 action). Direct CLI invokes method with action as $1.
The UI generates positive request IDs at most 1,000,000,000 for 32-bit transport compatibility; backend job lookup also accepts historical decimal IDs up to 15 digits.
Methods: tailscale_config [web_submit|start|stop|restart|start_nat]; tailscale_fettle []; tailscale_tsnets [1]; tailscale_status []; tailscale_ncheck []; tailscale_core [check|update|rollback]; tailscale_job [job-id]; tailscale_diagnostics [].
Mutating/diagnostic operations use numeric request id as job id, immediately respond via http_response with JSON string {accepted:true,job_id:"..."} after lock acceptance; lock contention {accepted:false,error:"busy"}.
Jobs: {schema:1,id,state:"running|success|failed|rolled_back",phase,message,updated_at,version?,previous_version?}. Atomic write. Log is separate display data. tailscale_job returns job object as JSON string. UI should parse response.result whether object or JSON string.
Status result: {schema:1,enabled:boolean,plugin_version:"3.0.0",core_version:string,backend_state:string,online:boolean|null,health_codes:[],health_messages:[],auth_url:string,monitoring_available:boolean,watchdog:{enabled:boolean,last_recovery:string,count_24h:number},core:{installed:string,available:string,can_rollback:boolean},error?:string}.
tsnets result: {interfaces:[{if,ip,rx,tx}]}. Diagnostics results can use jobs + sanitized log (tailnet keys/auth tokens excluded).
Frontend reads existing six bool keys + tailscale_watchdog_enable via GET /_api/tailscale_. API v3 saves send method tailscale_config, params ["web_submit", "<seven-bit snapshot>"], and fields: {}. Bit order is tailscale_enable, tailscale_ipv4_enable, tailscale_ipv6_enable, tailscale_advertise_routes, tailscale_accept_routes, tailscale_exit_node, tailscale_watchdog_enable. The snapshot must match ^[01]{7}$; backend validates and applies it only after acquiring the lifecycle lock. This avoids httpdb writing DBus fields before script dispatch/lock acceptance. Legacy web_submit without a snapshot remains supported by the backend for compatibility. Update and read methods send no DBus fields. No auto-install of core.

## Helper commands

- timeout SECONDS COMMAND [ARGS...] -> preserves exit code; deadline 124; no shell interpretation.
- version BINARY -> first validated version line; sets TS_BE_CLI=1 with 5s deadline.
- status SOCKET -> sanitized JSON {ok,version,backend_state,online,health_codes,health_messages,auth_url,ips,node_id,have_node_key,want_running,logged_out,sync_enabled,monitoring_available}; bounded LocalAPI requests, no raw state.
- fetch URL DEST MAX_BYTES -> bounded HTTPS download, allowed GitHub repository/release/CDN hosts only.
- verify ENVELOPE PUBKEY ARCH -> verified flattened descriptor JSON (below), fail closed.
- extract ARCHIVE DESCRIPTOR DEST -> checks archive SHA/size; one regular tailscale.combined only; max 12MiB, SHA/ELF arch, safe staging.
- keygen PRIVATE_FILE PUBLIC_FILE; sign PAYLOAD PRIVATE_FILE ENVELOPE (host/CI only, Ed25519 keys hex).
- json-get FILE FIELD supports fixed dot paths and numeric array indices; quote STRING produces JSON string encoding.
- atomic-link TARGET LINK replaces a relative cores/... pointer and synchronizes its parent directory.
- log PATH MAX_BYTES consumes stdin, redacts credentials and rotates bounded log files.

## Signed feed/core package

Envelope JSON {payload:"<base64 exact payload bytes>",signature:"<base64 Ed25519>"}.
Payload {schema:1,channel:"stable",version:"1.102.4",build:"r1",source_commit:"<40 hex>",recipe_sha256:"<64 hex>",created_at:"UTC ISO8601",artifacts:{arm:{url,size,sha256,unpacked_size,binary_sha256},arm64:{...}}}.
URLs https://github.com/thetapilla/tailscale-koolshare/releases/download/core-v<VERSION>-<BUILD>/tailscale-core_<VERSION>_<BUILD>_<ARCH>.tar.gz .
Verified descriptor flattens chosen artifact plus version/build/arch/source_commit/recipe_sha256. Manifest max 64KiB, archive max 13MiB, combined max 12MiB.
Core tar.gz contains exactly regular file tailscale.combined (0755), no extra paths. Metadata outside archive is signed. Both CLI symlinks created by installer/updater.
Stable feed rejects older version/build except explicit local rollback. Releases immutable; core-stable mutable signed envelope only.

## Build/package

Initial official core v1.102.4, Go1.26.6; CGO=0 arm GOARM=7 / arm64 GOARM64=v8.0.
Tags: ts_omit_aws,ts_omit_bird,ts_omit_tap,ts_omit_kube,ts_omit_completion,ts_include_cli,ts_omit_systray,ts_omit_tpm,ts_omit_debugeventbus,ts_omit_syspolicy,ts_omit_capture,ts_omit_ssh,ts_omit_taildrop.
UPX5.0.2 --best --lzma. Build outputs build/cores/<arch>/tailscale.combined and helper build/helpers/<arch>/tsks-helper.
Packages include top-level tailscale/ with plugin/ files, payload/<arch>/{tailscale.combined,tsks-helper,descriptor.json}; release.pub; version; .valid; plugin files checksum manifest.
Platform mapping hnd,qca,ipq32 -> arm; mtk,ipq64 -> arm64. Universal carries both arch payloads and five .valid lines. Final installed payload only chosen architecture.
Output ../dist/tailscale_3.0.0_{universal,hnd,qca,ipq32,ipq64,mtk}.tar.gz + SHA256SUMS. Per-platform build staging, deterministic archives. Plugin metadata must be3.0.0 before archive.
