#!/bin/sh
# Run only inside the disposable, network-isolated Linux smoke container.
set -eu
BIN=/input/tailscale.combined
EXPECTED=$1
mkdir -p /tmp/tsks-smoke
ln -s "$BIN" /tmp/tsks-smoke/tailscale
ln -s "$BIN" /tmp/tsks-smoke/tailscaled
CLI=/tmp/tsks-smoke/tailscale
DAEMON=/tmp/tsks-smoke/tailscaled
SOCKET=/tmp/tsks-smoke/tailscaled.sock
test "$("$CLI" version | head -n 1)" = "$EXPECTED"
test "$("$DAEMON" --version | head -n 1)" = "$EXPECTED"
test "$(/helper/tsks-helper version "$BIN")" = "$EXPECTED"
"$CLI" up --help > /tmp/tsks-smoke/up-help 2>&1
for option in accept-routes advertise-routes advertise-exit-node; do
    grep -q -- "--$option" /tmp/tsks-smoke/up-help
done
"$CLI" netcheck --help > /dev/null 2>&1
"$DAEMON" --tun=userspace-networking --state=mem: --socket="$SOCKET" --port=0 > /tmp/tsks-smoke/daemon.log 2>&1 &
PID=$!
trap 'kill "$PID" 2>/dev/null || true; wait "$PID" 2>/dev/null || true' EXIT HUP INT TERM
N=0
while [ ! -S "$SOCKET" ]; do
    kill -0 "$PID"
    N=$((N + 1))
    if [ "$N" -gt 30 ]; then cat /tmp/tsks-smoke/daemon.log; exit 1; fi
    sleep 1
done
# Logged-out status can exit nonzero; the JSON and process are checked separately.
"$CLI" --socket="$SOCKET" status --json > /tmp/tsks-smoke/status.json || true
grep -q '"BackendState": "NeedsLogin"' /tmp/tsks-smoke/status.json
grep -q '"Version": "'"$EXPECTED" /tmp/tsks-smoke/status.json
/helper/tsks-helper status "$SOCKET" > /tmp/tsks-smoke/helper-status.json
grep -q '"ok":true' /tmp/tsks-smoke/helper-status.json
grep -q '"backend_state":"NeedsLogin"' /tmp/tsks-smoke/helper-status.json
"$CLI" --socket="$SOCKET" set --accept-routes=true --advertise-routes=192.0.2.0/24 --advertise-exit-node=true
kill -0 "$PID"
printf 'PASS: CLI, daemon, packed helper, LocalAPI, route flags, exit-node flags, netcheck CLI: %s\n' "$EXPECTED"
