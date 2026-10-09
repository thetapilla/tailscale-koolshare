#!/bin/sh
# Disposable, network-isolated tests of real compressed cores and LocalAPI.
set -eu
BIN=/input/tailscale.combined
EXPECTED=$1
OMIT_UPDATE=$2
HELPER=/helper/tsks-helper
BASE=/tmp/tsks-smoke
mkdir -p "$BASE"
PID=
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
stop() {
    if [ -n "$PID" ]; then kill "$PID" 2>/dev/null || :; wait "$PID" 2>/dev/null || :; PID=; fi
}
trap stop EXIT HUP INT TERM
start() {
    CASE=$BASE/$1
    mkdir -p "$CASE"
    ln -sf "$BIN" "$CASE/tailscale"
    ln -sf "$BIN" "$CASE/tailscaled"
    CLI=$CASE/tailscale SOCKET=$CASE/tailscaled.sock STATE=$CASE/tailscaled.state
    "$CASE/tailscaled" --tun=userspace-networking --state="$STATE" --socket="$SOCKET" --port=0 >>"$CASE/daemon.log" 2>&1 &
    PID=$!
    n=0
    until "$CLI" --socket="$SOCKET" debug prefs >"$CASE/prefs.json" 2>/dev/null; do
        kill -0 "$PID" || { cat "$CASE/daemon.log"; fail 'daemon exited'; }
        n=$((n+1)); [ "$n" -le 30 ] || fail 'LocalAPI startup timeout'
        sleep 1
    done
}
get() { "$HELPER" json-get "$CASE/prefs.json" "$1"; }
prefs() { "$CLI" --socket="$SOCKET" debug prefs >"$CASE/prefs.json"; }
managed_set() {
    set -- set --netfilter-mode=on --accept-routes=true --advertise-exit-node=true \
        --advertise-routes=192.0.2.0/24 --auto-update=false --update-check=false "$@"
    "$CLI" --socket="$SOCKET" "$@"
}
check_managed() {
    prefs
    [ "$(get CorpDNS)" = "$1" ] || fail 'DNS preference changed'
    [ "$(get RouteAll)" = true ] || fail 'accept-routes preference changed'
    [ "$(get WantRunning)" = true ] || fail 'connect did not retain running intent'
    [ "$(get AutoUpdate.Apply)" = false ] && [ "$(get AutoUpdate.Check)" = false ] || fail 'updater preferences not disabled'
    routes=$(get AdvertiseRoutes)
    for route in '"0.0.0.0/0"' '"::/0"' '"192.0.2.0/24"'; do
        printf '%s\n' "$routes" | grep -Fq "$route" || fail 'advertised route lost'
    done
    ! grep -Fq 'reason: [opts.UpdatePrefs]' "$CASE/daemon.log" || fail 'preferences were replaced through Start'
}

start fresh
[ "$("$CLI" version | head -n 1)" = "$EXPECTED" ]
[ "$("$CASE/tailscaled" --version | head -n 1)" = "$EXPECTED" ]
[ "$("$HELPER" version "$BIN")" = "$EXPECTED" ]
"$CLI" up --help >"$CASE/up-help" 2>&1
for option in accept-routes advertise-routes advertise-exit-node; do grep -q -- "--$option" "$CASE/up-help"; done
"$CLI" netcheck --help >/dev/null 2>&1
"$CLI" --socket="$SOCKET" set --hostname=unmanaged-host
managed_set --accept-dns=false
"$HELPER" connect "$SOCKET" >"$CASE/connect.json"
check_managed false
[ "$(get Hostname)" = unmanaged-host ] || fail 'connect reset an unmanaged preference'
"$HELPER" status "$SOCKET" >"$CASE/helper-status.json"
[ "$("$HELPER" json-get "$CASE/helper-status.json" version)" = "$EXPECTED" ]
case $("$HELPER" json-get "$CASE/helper-status.json" version_long) in "$EXPECTED"-t*) ;; *) fail 'missing real source stamp';; esac
printf 'PASS: first set/connect preserves DNS, routes, exit node, updater and unmanaged preferences: %s\n' "$EXPECTED"
if [ "$OMIT_UPDATE" = 1 ]; then
    if "$CLI" update --help >"$CASE/update-help" 2>&1; then fail 'omitted update command remains available'; fi
    if "$CLI" --socket="$SOCKET" set --auto-update=true >"$CASE/enable-update.log" 2>&1; then fail 'omitted auto updater accepted Apply=true'; fi
    grep -Fq 'Auto-update support is disabled in this build' "$CASE/enable-update.log"
    printf 'PASS: standalone update command absent and auto-update=true rejected\n'
fi
stop

# A nonempty {} file is still a never-authenticated identity. Preferences set
# before login are not persisted and must be applied again after restart.
start empty-restart
managed_set --accept-dns=false
[ "$(tr -d '[:space:]' <"$STATE")" = '{}' ] || fail 'fixture did not create the nonempty empty state'
stop
start empty-restart
"$HELPER" status "$SOCKET" >"$CASE/before.json"
[ "$("$HELPER" json-get "$CASE/before.json" have_node_key)" != true ] || fail 'unexpected node key before login'
managed_set --accept-dns=false
"$HELPER" connect "$SOCKET" >/dev/null
check_managed false
printf 'PASS: nonempty {} state and pre-login restart retain managed preferences\n'
stop

# Negative control: the real bare up path replaces newly set preferences.
start bare-up-control
managed_set --accept-dns=false
"$HELPER" timeout 3 "$CLI" --socket="$SOCKET" up >/dev/null 2>&1 || :
prefs
[ "$(get CorpDNS)" = true ] && [ "$(get RouteAll)" = false ] || fail 'bare-up negative control did not reproduce preference replacement'
grep -Fq 'reason: [opts.UpdatePrefs]' "$CASE/daemon.log"
printf 'PASS: real bare up negative control reproduces preference replacement\n'
stop

migration() {
    name=$1 omitted=$2
    mkdir -p "$BASE/$name"
    /fixture/mkstate "$BASE/$name/tailscaled.state" true
    start "$name"
    [ "$(get AutoUpdate.Apply)" = true ] || fail 'synthetic identity did not retain Apply=true'
    key_before=$("$HELPER" json-get "$STATE" _machinekey)
    if [ "$omitted" = 1 ]; then
        if "$CLI" --socket="$SOCKET" set --accept-routes=true >"$CASE/old-set.log" 2>&1; then fail 'unsafe migration negative control unexpectedly succeeded'; fi
        grep -Fq 'Auto-update support is disabled in this build' "$CASE/old-set.log"
    fi
    # Exactly one production-style set simultaneously fixes Apply and changes
    # the managed flags, preserving the authenticated profile's DNS choice.
    managed_set
    "$HELPER" connect "$SOCKET" >/dev/null
    check_managed true
    [ "$(get Hostname)" = core-smoke ] || fail 'existing unmanaged hostname changed'
    [ "$("$HELPER" json-get "$STATE" _machinekey)" = "$key_before" ] || fail 'synthetic machine identity changed'
    printf 'PASS: %s synthetic logged-in Apply=true migration via one managed set\n' "$name"
    stop
}
migration candidate "$OMIT_UPDATE"
if [ -f /reference/manifest.json ]; then
    "$HELPER" verify /reference/manifest.json /release.pub "$3" >"$BASE/reference.json"
    url=$("$HELPER" json-get "$BASE/reference.json" url)
    "$HELPER" extract "/reference/${url##*/}" "$BASE/reference.json" "$BASE/reference"
    BIN=$BASE/reference/tailscale.combined
    migration reference 0
fi
printf 'PASS: actual core LocalAPI and migration smoke complete: %s\n' "$EXPECTED"
