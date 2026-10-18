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
        --advertise-routes=192.0.2.0/24,198.51.100.27/32,2001:db8:60::/64 --auto-update=false --update-check=false "$@"
    "$CLI" --socket="$SOCKET" "$@"
}
check_managed() {
    prefs
    [ "$(get CorpDNS)" = "$1" ] || fail 'DNS preference changed'
    [ "$(get RouteAll)" = true ] || fail 'accept-routes preference changed'
    [ "$(get WantRunning)" = true ] || fail 'connect did not retain running intent'
    [ "$(get AutoUpdate.Apply)" = false ] && [ "$(get AutoUpdate.Check)" = false ] || fail 'updater preferences not disabled'
    routes=$(get AdvertiseRoutes)
    for route in '"0.0.0.0/0"' '"::/0"' '"192.0.2.0/24"' '"198.51.100.27/32"' '"2001:db8:60::/64"'; do
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
advertised=$("$HELPER" json-get "$CASE/helper-status.json" routes.advertised)
for route in '"192.0.2.0/24"' '"198.51.100.27/32"' '"2001:db8:60::/64"'; do
    printf '%s\n' "$advertised" | grep -Fq "$route" || fail 'helper omitted advertised subnet'
done
! printf '%s\n' "$advertised" | grep -Fq '"0.0.0.0/0"' || fail 'helper included IPv4 exit-node default'
! printf '%s\n' "$advertised" | grep -Fq '"::/0"' || fail 'helper included IPv6 exit-node default'
printf 'PASS: first set/connect preserves DNS, routes, exit node, updater and unmanaged preferences: %s\n' "$EXPECTED"
managed_set --accept-dns=true
check_managed true
managed_set --accept-dns=false
check_managed false
[ "$(get Hostname)" = unmanaged-host ] || fail 'DNS toggle reset an unmanaged preference'
printf 'PASS: explicit DNS true/false retains merged IPv4/IPv6 subnets and exit-node defaults\n'
# The connector is enabled in a network-isolated daemon. The real LocalAPI
# must accept the feature, expose learning data, and keep route preferences
# untouched when the planner decides no route edit is needed.
"$CLI" --socket="$SOCKET" set --advertise-connector=true
prefs
[ "$(get AppConnector.Advertise)" = true ] || fail 'connector preference not applied'
"$CLI" --socket="$SOCKET" appc-routes --map >"$CASE/appc-map.json"
plan=$("$HELPER" routes-plan "$SOCKET" '192.0.2.0/24,198.51.100.27/32,2001:db8:60::/64' 1 1)
[ "$plan" = keep ] || fail 'unchanged routes planned an edit'
routes_before=$(get AdvertiseRoutes)
edits_before=$(grep -c 'EditPrefs:.*AdvertiseRoutes' "$CASE/daemon.log" || :)
"$CLI" --socket="$SOCKET" set --netfilter-mode=on --accept-routes=true --accept-dns=true --advertise-connector=true --auto-update=false --update-check=false
prefs
[ "$(get AdvertiseRoutes)" = "$routes_before" ] || fail 'unrelated set changed advertised prefixes'
[ "$(grep -c 'EditPrefs:.*AdvertiseRoutes' "$CASE/daemon.log" || :)" = "$edits_before" ] || fail 'unrelated set edited route preferences'
"$HELPER" status "$SOCKET" >"$CASE/appc-status.json"
[ "$("$HELPER" json-get "$CASE/appc-status.json" app_connector.advertise)" = true ] || fail 'helper lost connector flag'
[ -z "$("$HELPER" json-get "$CASE/appc-status.json" app_connector.error)" ] || fail 'connector learning endpoint unavailable'
"$CLI" --socket="$SOCKET" set --advertise-connector=false --accept-dns=false
printf 'PASS: real connector prefs and learning endpoint; keep plan avoids AdvertiseRoutes edit\n'
if [ "$OMIT_UPDATE" = 1 ]; then
    if "$CLI" update --help >"$CASE/update-help" 2>&1; then fail 'omitted update command remains available'; fi
    if "$CLI" --socket="$SOCKET" set --auto-update=true >"$CASE/enable-update.log" 2>&1; then fail 'omitted auto updater accepted Apply=true'; fi
    grep -Fq 'Auto-update support is disabled in this build' "$CASE/enable-update.log"
    printf 'PASS: standalone update command absent and auto-update=true rejected\n'
fi
stop

# Seed only synthetic persisted app-connector records. Offline tests cannot
# model a live control map, but can check preservation through real LocalAPI
# preference edits and daemon restart without exposing any real identity.
mkdir -p "$BASE/appc-persisted"
/fixture/mkstate "$BASE/appc-persisted/tailscaled.state" false appc
start appc-persisted
"$CLI" --socket="$SOCKET" appc-routes --map >"$CASE/learned-before.json"
grep -Fq '198.51.100.7' "$CASE/learned-before.json" || fail 'persisted learning fixture missing'
learned_before=$("$HELPER" sha256 "$CASE/learned-before.json")
for managed in '192.0.2.0/24,198.51.100.7/32' '192.0.2.0/24'; do
    [ "$("$HELPER" routes-plan "$SOCKET" "$managed" 0 1)" = keep ] || fail 'overlapping custom removal lost learned ownership'
done
"$CLI" --socket="$SOCKET" set --accept-dns=false --advertise-connector=true --auto-update=false --update-check=false
"$CLI" --socket="$SOCKET" appc-routes --map >"$CASE/learned-after.json"
[ "$("$HELPER" sha256 "$CASE/learned-after.json")" = "$learned_before" ] || fail 'unrelated settings changed persisted learning'
! grep -q 'EditPrefs:.*AdvertiseRoutes' "$CASE/daemon.log" || fail 'unrelated settings wrote advertised prefixes'
stop
start appc-persisted
"$CLI" --socket="$SOCKET" appc-routes --map >"$CASE/learned-restart.json"
[ "$("$HELPER" sha256 "$CASE/learned-restart.json")" = "$learned_before" ] || fail 'restart changed persisted learning'
[ "$("$HELPER" routes-plan "$SOCKET" '192.0.2.0/24' 0 1)" = keep ] || fail 'restart lost learned or Control prefixes'
printf 'PASS: persisted connector learning and Control ownership survive unrelated set and restart\n'
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
    # Exactly one production-style set fixes Apply and applies the configured
    # DNS choice, including identities which previously enabled CorpDNS.
    managed_set --accept-dns=false
    "$HELPER" connect "$SOCKET" >/dev/null
    check_managed false
    managed_set --accept-dns=true
    check_managed true
    managed_set --accept-dns=false
    check_managed false
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
