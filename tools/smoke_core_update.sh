#!/bin/sh
# Real signed core transactions in a disposable network-isolated container.
set -eu
umask 077
export LC_ALL=C
arch=$1
mode=$2
base=/tmp/core-update-smoke
export TSKS_ROOT=$base/koolshare TSKS_RUN=$base/run TSKS_WEB=$base/web
export TSKS_SMOKE_CONFIG=$base/dbus
mkdir -p "$base/commands" "$TSKS_ROOT/bin" "$TSKS_ROOT/tailscale/cores" "$TSKS_SMOKE_CONFIG"
ln -s /helper/tsks-helper "$TSKS_ROOT/bin/tsks-helper"

# Match the restricted runtime dependency set used by the installer fixtures.
for name in awk cat chmod cp date dirname find flock grep ln ls mkdir mv readlink rm rmdir sed sleep sync tail tr wc which; do
    source=$(command -v "$name")
    ln -s "$source" "$base/commands/$name"
done
cat >"$base/commands/mock" <<'MOCK'
#!/bin/sh
set -eu
case ${0##*/} in
    dbus)
        action=$1; value=${2:-}; key=${value%%=*}
        case $key in ''|*[!a-zA-Z0-9_]*) exit 1;; esac
        case $action in
            get) [ ! -f "$TSKS_SMOKE_CONFIG/$key" ] || cat "$TSKS_SMOKE_CONFIG/$key";;
            set) printf '%s' "${value#*=}" >"$TSKS_SMOKE_CONFIG/$key";;
            remove) rm -f "$TSKS_SMOKE_CONFIG/$key";;
            *) exit 1;;
        esac;;
    nvram)
        [ "$1" = get ] || exit 1
        case $2 in lan_ifname) printf 'br0\n';; lan_ipaddr) printf '192.0.2.1\n';;
            lan_netmask) printf '255.255.255.0\n';; *) exit 1;; esac;;
    iptables|ip6tables)
        for arg do
            case $arg in -w*|--wait*) exit 2;;
                -h|--help) printf 'iptables v1.4.15\nUsage: iptables -A -C -D -F -N -X\n'; exit 0;;
                -D|-C) exit 1;; esac
        done;;
    iptables-save|ip6tables-save|cru) :;;
    *) exit 1;;
esac
MOCK
chmod 755 "$base/commands/mock"
for name in dbus nvram iptables ip6tables iptables-save ip6tables-save cru; do
    ln -s mock "$base/commands/$name"
done
PATH=$base/commands
export PATH
. /plugin/scripts/tailscale_lib.sh
. /plugin/scripts/tailscale_core_lib.sh
ts_init
ts_lock
cleanup() {
    rc=$?
    trap - EXIT HUP INT TERM
    ts_stop >/dev/null 2>&1 || :
    ts_unlock
    if [ "$rc" != 0 ]; then
        [ ! -f "$RUN/events.log" ] || cat "$RUN/events.log" >&2
        [ ! -f "$RUN/daemon.log" ] || cat "$RUN/daemon.log" >&2
    fi
    exit "$rc"
}
trap cleanup EXIT HUP INT TERM
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

prepare_core() {
    release=$1
    "$HELPER" verify "$release/manifest.json" /plugin/release.pub "$arch" >"$base/descriptor.json"
    url=$(ts_get "$base/descriptor.json" url)
    version=$(ts_get "$base/descriptor.json" version)
    build=$(ts_get "$base/descriptor.json" build)
    target=cores/$version-$build-$arch
    "$HELPER" extract "$release/${url##*/}" "$base/descriptor.json" "$DATA/$target"
    ts_core_valid "$DATA/$target" || fail 'actual core does not match its signed descriptor'
    # Only the daemon entry is wrapped, to add a userspace TUN. The combined
    # binary, CLI, LocalAPI, helper and transaction functions are unmodified.
    rm "$DATA/$target/tailscaled"
    cat >"$DATA/$target/tailscaled" <<'DAEMON'
#!/bin/sh
exec "$DATA/current/tailscale.combined" --tun=userspace-networking "$@"
DAEMON
    chmod 755 "$DATA/$target/tailscaled"
}
prepare_core /previous
old=$target old_version=$version old_build=$build
prepare_core /candidate
new=$target new_version=$version new_build=$build
comparison=$("$HELPER" compare "$new_version" "$old_version")
if [ "$comparison" = 0 ]; then
    [ "${new_build#r}" -gt "${old_build#r}" ] || fail 'candidate build must increase for an unchanged version'
else
    [ "$comparison" = 1 ] || fail 'candidate must be newer than previous core'
fi
"$HELPER" atomic-link "$old" "$DATA/current"
printf '1' >"$TSKS_SMOKE_CONFIG/tailscale_enable"
printf '0' >"$TSKS_SMOKE_CONFIG/tailscale_advertise_routes"
if [ "$mode" = update ]; then
    printf '1' >"$TSKS_SMOKE_CONFIG/tailscale_accept_dns"
    printf '1' >"$TSKS_SMOKE_CONFIG/tailscale_advertise_routes"
    printf '1' >"$TSKS_SMOKE_CONFIG/tailscale_custom_routes_enable"
    printf '%s' '198.51.100.27/32,2001:db8:60::/64,192.0.2.0/24' >"$TSKS_SMOKE_CONFIG/tailscale_custom_routes"
fi

check_live() {
    expected=$1
    ts_pid_alive || fail 'daemon is not running'
    ts_status_file "$RUN/test-status.json" || fail 'real LocalAPI request failed'
    [ "$(ts_get "$RUN/test-status.json" ok)" = true ] || fail 'real LocalAPI is unavailable'
    [ "$(ts_get "$RUN/test-status.json" version)" = "$expected" ] || fail 'LocalAPI short version differs from release'
    actual=$(ts_get "$RUN/test-status.json" version_long)
    # Require a real long version, so a fixture returning only the manifest's
    # short version cannot accidentally make this regression test pass.
    case $actual in "$expected"-t*) ;; *) fail 'daemon did not expose its real build suffix';; esac
    ts_cli status --json >"$RUN/test-raw-status.json" || :
    [ "$(ts_get "$RUN/test-raw-status.json" Version)" = "$actual" ] || fail 'helper changed the original long version'
    [ "$(ts_get "$RUN/test-status.json" want_running)" = true ] || fail 'running preference changed'
    case $(ts_get "$RUN/test-status.json" backend_state) in NoState|Starting|Running) ;; *) fail 'synthetic authenticated identity was lost';; esac
    [ "$(ts_get "$RUN/test-status.json" have_node_key)" = true ] || fail 'synthetic node key was lost'
    [ "$(ts_get "$RUN/test-status.json" logged_out)" = false ] || fail 'synthetic profile was logged out'
    if [ "$(ts_get "$RUN/test-status.json" backend_state)" = NoState ]; then
        [ "$(ts_get "$RUN/test-status.json" health_available)" = true ] || fail 'offline startup has no structured health evidence'
        case $(ts_get "$RUN/test-status.json" health_codes) in *'"state-store-health"'*) fail 'offline startup has a state-store failure';; esac
    fi
    printf 'LocalAPI %s: %s\n' "$arch" "$actual"
}
check_identity() {
    # This key is generated by this disposable daemon. Compare silently and
    # never emit raw state or credentials in test output.
    [ "$(ts_get "$STATE" _machinekey)" = "$machine_key" ] || fail 'generated machine identity changed'
}
check_preferences() {
    ts_cli debug prefs >"$RUN/checked-prefs.json"
    [ "$(ts_get "$RUN/checked-prefs.json" CorpDNS)" = true ] || fail 'DNS did not follow the enabled plugin setting'
    [ "$(ts_get "$RUN/checked-prefs.json" Hostname)" = core-smoke ] || fail 'unmanaged hostname changed'
    # ipn.Prefs.Persist is serialized under its historical JSON key Config.
    [ "$(ts_get "$RUN/checked-prefs.json" Config.NodeID)" = nTEST ] || fail 'synthetic node identity changed'
    [ "$(ts_get "$RUN/checked-prefs.json" AutoUpdate.Apply)" = false ] && [ "$(ts_get "$RUN/checked-prefs.json" AutoUpdate.Check)" = false ] || fail 'updater preferences not normalized'
    [ "$(ts_get "$RUN/checked-prefs.json" RouteAll)" = true ] || fail 'accept-routes preference changed'
    [ "$(ts_get "$RUN/checked-prefs.json" AdvertiseRoutes)" = '["192.0.2.0/24","198.51.100.27/32","2001:db8:60::/64"]' ] || fail 'LAN and custom IPv4/IPv6 route union changed'
}
check_committed() {
    [ "$(ts_core_target "$DATA/current")" = "$1" ] || fail 'transaction selected wrong core'
    [ "$(ts_core_target "$DATA/previous")" = "$2" ] || fail 'previous core was not retained'
    [ ! -e "$DATA/update.txn" ] || fail 'transaction journal remains after success'
    [ ! -e "$DATA/update.state" ] || fail 'transaction state snapshot remains after success'
    check_identity
}

ts_job_begin 31001
/fixture/mkstate "$STATE" true
ts_start
check_live "$old_version"
machine_key=$(ts_get "$STATE" _machinekey)
[ -n "$machine_key" ] && [ "$machine_key" != null ] || fail 'fixture did not create a machine identity'
if [ "$mode" = update ]; then
    check_preferences
    # The managed setting, rather than an old daemon preference, must win
    # after each restart involved in a transaction.
    ts_cli set --accept-dns=false --advertise-routes=
fi
# Seed the old running core with a real persisted Apply=true. This reproduces
# the tailnet default adopted by older plugin versions before the transaction.
ts_cli set --auto-update=true --update-check=true
ts_cli debug prefs >"$RUN/before-prefs.json"
[ "$(ts_get "$RUN/before-prefs.json" AutoUpdate.Apply)" = true ] || fail 'migration fixture did not retain Apply=true'
if [ "$mode" = update ]; then
    [ "$(ts_get "$RUN/before-prefs.json" CorpDNS)" = false ] || fail 'DNS negative control did not differ from the configured setting'
fi
if [ "$mode" = legacy-rollback ]; then
    # The previous plugin cannot cold-start a connected profile without a
    # control map. Keep the daemon/service enabled but deliberately disconnect
    # this baseline profile, isolating its Apply=true migration failure from
    # that separate offline NoState lifecycle limitation.
    ts_cli down
    ts_cli debug prefs >"$RUN/legacy-disconnected-prefs.json"
    [ "$(ts_get "$RUN/legacy-disconnected-prefs.json" WantRunning)" = false ] || fail 'legacy baseline did not disconnect'
    if ts_core_switch "$new"; then fail 'old plugin unexpectedly accepted the incompatible updater preference'; else result=$?; fi
    [ "$result" = 2 ] || fail 'old plugin did not complete automatic rollback'
    [ "$(ts_core_target "$DATA/current")" = "$old" ] || fail 'old plugin failed to restore original core'
    [ ! -e "$DATA/update.txn" ] && [ ! -e "$DATA/update.state" ] || fail 'rollback transaction remains incomplete'
    check_live "$old_version"
    check_identity
    ts_cli debug prefs >"$RUN/restored-prefs.json"
    [ "$(ts_get "$RUN/restored-prefs.json" AutoUpdate.Apply)" = true ] || fail 'rollback did not restore original preferences'
    printf 'PASS %s: previous plugin safely restores %s-%s when candidate rejects Apply=true (service enabled, profile deliberately disconnected)\n' "$arch" "$old_version" "$old_build"
    exit 0
fi
ts_core_switch "$new" || fail 'enabled update rejected a healthy real daemon'
check_committed "$new" "$old"
check_live "$new_version"
check_preferences
printf 'PASS %s: enabled %s-%s -> %s-%s applies configured DNS/routes and preserves identity and hostname\n' "$arch" "$old_version" "$old_build" "$new_version" "$new_build"

ts_cli set --accept-dns=false --advertise-routes=
ts_core_rollback || fail 'manual rollback failed'
check_committed "$old" "$new"
check_live "$old_version"
check_preferences
printf 'PASS %s: enabled manual rollback restores configured DNS/routes on the previous real daemon\n' "$arch"

ts_cli set --accept-dns=false --advertise-routes=
ts_stop
! ts_pid_alive || fail 'daemon survived explicit stop'
printf '0' >"$TSKS_SMOKE_CONFIG/tailscale_enable"
before=$("$HELPER" sha256 "$STATE")
ts_core_switch "$new" || fail 'disabled update failed'
check_committed "$new" "$old"
! ts_pid_alive || fail 'disabled update started daemon'
[ "$("$HELPER" sha256 "$STATE")" = "$before" ] || fail 'disabled update changed stopped state'
printf '1' >"$TSKS_SMOKE_CONFIG/tailscale_enable"
ts_start
check_live "$new_version"
check_identity
check_preferences
ts_stop
! ts_pid_alive || fail 'candidate daemon survived final stop'
printf 'PASS %s: disabled update leaves state unchanged; manual start applies configured DNS/routes\n' "$arch"
