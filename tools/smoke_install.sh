#!/bin/sh
# Archive integration with the real installer/library/helper/core. Only the
# software-center and network environment are simulated. Install cases keep
# Tailscale disabled; the optional lifecycle case uses a userspace daemon.
set -eu
umask 077
export LC_ALL=C
version=$1 arch=$2 mode=$3
shift 3
base=/tmp/install-smoke
mkdir -p "$base/commands"

# Do not inherit the image's extensive PATH. In particular, od, timeout,
# mkfifo and mktemp are unavailable to the installer and installed scripts.
for name in awk cat chmod cp date dirname find flock grep ln ls mkdir mv readlink rm rmdir sed sha256sum sleep tail tr wc which; do
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
        case $2 in
            productid) printf '%s\n' "$TSKS_SMOKE_MODEL";; odmpid) :;;
            lan_ifname) printf 'br0\n';; lan_ipaddr) printf '192.0.2.1\n';;
            lan_netmask) printf '255.255.255.0\n';; *) exit 1;;
        esac;;
    uname) [ "$1" = -r ] && printf '4.19.183\n';;
    df) printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\nfixture 262144 32768 229376 13%% /tmp\n';;
    iptables|ip6tables)
        printf '%s %s\n' "${0##*/}" "$*" >>"${TSKS_SMOKE_CONFIG%/dbus}/firewall.calls"
        # Model a legacy interface which cannot accept the xtables wait flag.
        for arg do
            case $arg in
                -w*|--wait*)
                    printf 'unsupported xtables wait argument\n' >>"${TSKS_SMOKE_CONFIG%/dbus}/firewall.unsupported"
                    exit 2;;
                -h|--help) printf 'iptables v1.4.15\nUsage: iptables -A -C -D -F -N -X\n'; exit 0;;
            esac
        done
        # Rule state and packet forwarding are outside this fixture's scope.
        for arg do case $arg in -D|-C) exit 1;; esac; done;;
    iptables-save|ip6tables-save|cru) :;;
    *) exit 1;;
esac
MOCK
chmod 755 "$base/commands/mock"
for name in dbus nvram uname df iptables ip6tables iptables-save ip6tables-save cru; do
    ln -s mock "$base/commands/$name"
done

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
snapshot() {
    # Include all installed bytes, modes and link targets, excluding timestamps.
    # Paths come from the generated release, not a user-controlled filesystem.
    (cd "$1" && find . -mindepth 1 -printf '%P\n' | sort | while IFS= read -r path; do
        if [ -L "$path" ]; then
            printf 'link %s %s\n' "$path" "$(readlink "$path")"
        elif [ -d "$path" ]; then
            printf 'dir %s %s\n' "$(stat -c %a "$path")" "$path"
        else
            printf 'file %s %s\n' "$(stat -c %a "$path")" "$(sha256sum "$path")"
        fi
    done)
}
prepare() {
    case_root=$1
    mkdir -p "$case_root/koolshare/configs/tailscale" "$case_root/dbus" "$case_root/proc" "$case_root/run" "$case_root/package"
    printf 'fixture-identity-preserved\n' >"$case_root/koolshare/configs/tailscale/tailscaled.state"
    printf '0' >"$case_root/dbus/tailscale_enable"
    printf '0' >"$case_root/dbus/tailscale_accept_routes"
    printf 'MemAvailable: 131072 kB\n' >"$case_root/proc/meminfo"
    tar -xzf "/archives/tailscale_${version}_${2}.tar.gz" -C "$case_root/package"
}
install() {
    case_root=$1
    pkg=$case_root/package/tailscale
    # Match the software-center's textual package guard before invocation.
    ! grep -q 'detect_package\|ks_tar_install' "$pkg/install.sh" || fail 'host package guard rejected installer'
    if ! env PATH="$base/commands" TSKS_ROOT="$case_root/koolshare" \
            TSKS_RUN="$case_root/run" TSKS_WEB="$case_root/web" TSKS_PROC="$case_root/proc" \
            TSKS_SMOKE_CONFIG="$case_root/dbus" TSKS_SMOKE_MODEL="$model" \
            /bin/sh "$pkg/install.sh" >"$case_root/install.log" 2>&1; then
        if [ "$mode" = missing-od ]; then
            grep -q 'od: not found' "$case_root/install.log" || { cat "$case_root/install.log"; fail 'expected missing-od failure'; }
            [ ! -e "$case_root/koolshare/tailscale/current" ] || fail 'failed install selected a core'
            [ ! -e "$case_root/dbus/tailscale_version" ] || fail 'failed install registered its version'
            [ "$(cat "$case_root/koolshare/configs/tailscale/tailscaled.state")" = fixture-identity-preserved ] || fail 'failed install changed identity'
            printf 'Reproduced missing od: %s / %s\n' "$platform" "$2"
            return 0
        fi
        cat "$case_root/install.log" >&2
        fail "$platform / $2 installation failed"
    fi
    [ "$mode" != missing-od ] || fail 'older archive unexpectedly installed without od'
    ! grep -q 'not found' "$case_root/install.log" || { cat "$case_root/install.log"; fail 'installer swallowed a missing-command error'; }
}
validate() {
    case_root=$1
    ks=$case_root/koolshare
    core=$ks/tailscale/current
    expected=$case_root/package/tailscale/payload/$arch
    [ "$(cat "$case_root/dbus/tailscale_version")" = "$version" ] || fail 'plugin version registration'
    [ "$(cat "$case_root/dbus/softcenter_module_tailscale_install")" = 1 ] || fail 'software-center registration'
    [ "$(cat "$case_root/dbus/tailscale_enable")" = 0 ] || fail 'disabled setting changed'
    [ "$(cat "$case_root/dbus/tailscale_accept_routes")" = 0 ] || fail 'preference changed'
    [ "$(cat "$ks/configs/tailscale/tailscaled.state")" = fixture-identity-preserved ] || fail 'identity changed'
    [ "$(stat -c %a "$ks/configs/tailscale/tailscaled.state")" = 600 ] || fail 'identity mode'
    [ "$(stat -c %a "$ks/configs/tailscale")" = 700 ] || fail 'identity directory mode'
    [ "$(stat -c %a "$ks/bin/tsks-helper")" = 755 ] || fail 'helper mode'
    [ "$(stat -c %a "$core/tailscale.combined")" = 755 ] || fail 'core mode'
    for file in "$ks"/scripts/* "$ks"/init.d/*; do
        [ "$(stat -c %a "$file")" = 755 ] || fail 'script mode'
    done
    for file in "$ks"/webs/* "$ks"/res/*; do
        [ "$(stat -c %a "$file")" = 644 ] || fail 'web resource mode'
    done
    [ "$(readlink "$ks/bin/tailscale")" = ../tailscale/current/tailscale ] || fail 'CLI link'
    [ "$(readlink "$ks/bin/tailscaled")" = ../tailscale/current/tailscaled ] || fail 'daemon link'
    [ "$(readlink "$core/tailscale")" = tailscale.combined ] || fail 'combined CLI entry'
    [ "$(readlink "$core/tailscaled")" = tailscale.combined ] || fail 'combined daemon entry'
    cmp "$core/tailscale.combined" "$expected/tailscale.combined" || fail 'selected core bytes'
    cmp "$ks/bin/tsks-helper" "$expected/tsks-helper" || fail 'selected helper bytes'
    [ "$("$ks/bin/tsks-helper" json-get "$core/descriptor.json" arch)" = "$arch" ] || fail 'installed architecture'
    [ "$("$ks/bin/tsks-helper" version "$core/tailscale.combined")" = "$("$ks/bin/tsks-helper" json-get "$core/descriptor.json" version)" ] || fail 'actual core version'
    [ "$(find "$ks/tailscale/cores" -mindepth 1 -maxdepth 1 -type d | wc -l)" -eq 1 ] || fail 'extra core retained'
    [ ! -e "$ks/payload" ] || fail 'unselected archive payload installed'
    [ -z "$(find "$ks" -name '.tailscale-install.*' -print)" ] || fail 'installation stage remains'
    [ ! -e "$case_root/run/tailscaled.pid" ] || fail 'disabled install started daemon'
    [ ! -e "$case_root/run/tailscaled.sock" ] || fail 'disabled install opened LocalAPI'
    [ ! -e "$case_root/firewall.unsupported" ] || fail 'unsupported firewall option invoked'
}

lifecycle() {
    case_root=$1
    ks=$case_root/koolshare
    # Previous identity checks used a sentinel. This separate lifecycle case
    # starts with an empty state store and has no access to a real tailnet.
    rm "$ks/configs/tailscale/tailscaled.state"
    printf '1' >"$case_root/dbus/tailscale_enable"
    printf '0' >"$case_root/dbus/tailscale_advertise_routes"
    # The sole executable substitution adds a userspace TUN argument; the
    # actual core, settings CLI, helper and installed library run unchanged.
    rm "$ks/tailscale/current/tailscaled"
    cat >"$ks/tailscale/current/tailscaled" <<'DAEMON'
#!/bin/sh
exec "$TSKS_SMOKE_COMBINED" --tun=userspace-networking "$@"
DAEMON
    chmod 755 "$ks/tailscale/current/tailscaled"
    cat >"$case_root/lifecycle.sh" <<'LIFECYCLE'
#!/bin/sh
set -eu
. "$TSKS_ROOT/scripts/tailscale_lib.sh"
ts_init
ts_lan
[ "$LAN_CIDR" = 192.0.2.0/24 ]
ts_lock
trap 'ts_stop >/dev/null 2>&1 || :; ts_unlock' EXIT HUP INT TERM
ts_job_begin 12345
ts_job_write running starting 'Starting lifecycle fixture'
ts_job_log 'Testing FIFO logging and LocalAPI'
ts_start
ts_pid_alive
[ -p "$RUN/daemon.pipe" ]
[ "$(ts_get "$RUN/start-status.json" ok)" = true ]
case $(ts_get "$RUN/start-status.json" backend_state) in NeedsLogin|Starting) ;; *) exit 1;; esac
[ -s "$RUN/daemon.log" ]
ts_job_write success finished 'Lifecycle fixture passed'
[ "$(ts_get "$WEB/tailscale3_12345.json" state)" = success ]
ts_stop
! ts_pid_alive
[ ! -e "$RUN/daemon.pipe" ]
[ ! -e "$RUN/tailscaled.pid" ]
ts_unlock
trap - EXIT HUP INT TERM
LIFECYCLE
    if ! env PATH="$base/commands" TSKS_ROOT="$ks" TSKS_RUN="$case_root/run" TSKS_WEB="$case_root/web" \
            TSKS_PROC=/proc TSKS_SMOKE_CONFIG="$case_root/dbus" TSKS_SMOKE_MODEL="$model" \
            TSKS_SMOKE_COMBINED="$ks/tailscale/current/tailscale.combined" \
            /bin/sh "$case_root/lifecycle.sh" >"$case_root/lifecycle.log" 2>&1; then
        cat "$case_root/lifecycle.log" "$case_root/run/events.log" >&2
        fail 'enabled lifecycle fixture failed'
    fi
    [ ! -e "$case_root/firewall.unsupported" ] || fail 'lifecycle invoked unsupported firewall option'
    ! grep -q 'not found' "$case_root/lifecycle.log" || fail 'lifecycle swallowed a missing-command error'
    [ "$(stat -c %a "$case_root/web/tailscale3_12345.json")" = 644 ] || fail 'public job permissions'
    [ "$(stat -c %a "$case_root/run/daemon.log")" = 600 ] || fail 'private log permissions'
    [ "$(stat -c %a "$ks/configs/tailscale/tailscaled.state")" = 600 ] || fail 'new state permissions'
    printf 'PASS %s: enabled userspace daemon, real LocalAPI/settings, FIFO logger, public jobs and legacy firewall argv\n' "$arch"
}

for platform do
    case $platform in
        hnd) model=RT-AX88U;; qca) model=RT-AX89X;; ipq32) model=ZenWiFi_BD4;;
        ipq64) model=TUF_6500;; mtk) model=TX-AX6000;; *) fail 'unknown platform';;
    esac
    for package in universal "$platform"; do
        case_root=$base/$platform/$package
        prepare "$case_root" "$package"
        install "$case_root" "$package"
        [ "$mode" != missing-od ] || continue
        validate "$case_root"
        snapshot "$case_root/koolshare" >"$case_root/installed.tree"
        snapshot "$case_root/dbus" >"$case_root/installed.config"
        # Exercise preservation of an installed core and identity on reinstallation.
        install "$case_root" "$package (reinstall)"
        validate "$case_root"
        snapshot "$case_root/koolshare" >"$case_root/reinstalled.tree"
        snapshot "$case_root/dbus" >"$case_root/reinstalled.config"
        cmp "$case_root/installed.tree" "$case_root/reinstalled.tree" || fail 'reinstall changed installed files'
        cmp "$case_root/installed.config" "$case_root/reinstalled.config" || fail 'reinstall changed configuration'
    done
    if [ "$mode" != missing-od ]; then
        cmp "$base/$platform/universal/installed.tree" "$base/$platform/$platform/installed.tree" || fail 'universal and standalone installations differ'
        cmp "$base/$platform/universal/installed.config" "$base/$platform/$platform/installed.config" || fail 'universal and standalone configurations differ'
        # Import an actual combined core using the legacy on-disk layout.
        # A stopped, disabled daemon avoids simulated process/TUN behavior.
        case_root=$base/$platform/legacy
        prepare "$case_root" "$platform"
        mkdir -p "$case_root/koolshare/bin"
        cp -p "$case_root/package/tailscale/payload/$arch/tailscale.combined" "$case_root/koolshare/bin/tailscale.combined"
        ln -s tailscale.combined "$case_root/koolshare/bin/tailscale"
        ln -s tailscale.combined "$case_root/koolshare/bin/tailscaled"
        printf '2.0.0' >"$case_root/dbus/tailscale_version"
        install "$case_root" "$platform (legacy migration)"
        validate "$case_root"
        [ "$("$case_root/koolshare/bin/tsks-helper" json-get "$case_root/koolshare/tailscale/current/descriptor.json" origin)" = legacy ] || fail 'legacy core was replaced'
        [ ! -e "$case_root/koolshare/bin/tailscale.combined" ] || fail 'legacy combined file was not migrated'
        printf 'PASS %s (%s): actual payload install, reinstall, legacy migration, identity, modes and universal/standalone equivalence\n' "$platform" "$arch"
        if [ "${TSKS_SMOKE_LIFECYCLE:-0}" = 1 ]; then
            lifecycle "$base/$platform/universal"
            # One actual lifecycle run for each CPU architecture is enough;
            # platform mapping is covered separately for every package pair.
            TSKS_SMOKE_LIFECYCLE=0
        fi
    fi
    rm -rf "$base/$platform"
done
