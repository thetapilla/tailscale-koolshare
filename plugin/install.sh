#!/bin/sh
# Transactional Koolshare installer. BusyBox ash 1.25; no old plugin scripts run.
set -u
export LC_ALL=C
umask 077
PKG=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
KSROOT=${TSKS_ROOT:-/koolshare}
RUN=${TSKS_RUN:-/tmp/tailscale3}
DATA=$KSROOT/tailscale
STATE=$KSROOT/configs/tailscale/tailscaled.state
STAGE=$KSROOT/.tailscale-install.$$
TOUCHED=0
COMMITTED=0
LOCKED=0
STOPPED=0
NEW_CORE=
OLD_CORE=
OLD_SOCKET=/var/run/tailscale/tailscaled.sock
OLD_PORT=
OLD_KIND=none
say() { printf '%s\n' "$*"; }
fail() { say "安装失败：$*" >&2; exit 1; }

# Check every entry before sha256sum opens any manifest-supplied pathname.
check_package() {
    [ -f "$PKG/manifest.sha256" ] && [ ! -L "$PKG/manifest.sha256" ] || fail '缺少校验清单'
    [ -z "$(find "$PKG" -type l -print)" ] || fail '安装包不能包含符号链接'
    awk 'NF!=2 || length($1)!=64 || $1~/[^a-f0-9]/ || $2~/[^A-Za-z0-9_.\/-]/ || $2~/^\// || $2~/(^|\/)\.\.?($|\/)/ || $2~/\/\// || seen[$2]++ {exit 1} END {if(NR==0)exit 1}' "$PKG/manifest.sha256" || fail '校验清单格式无效'
    (cd "$PKG" && sha256sum -c manifest.sha256 >/dev/null 2>&1) || fail '安装包文件校验失败'
    find "$PKG" -type f | while IFS= read -r file; do
        rel=${file#"$PKG/"}
        [ "$rel" = manifest.sha256 ] && continue
        awk -v name="$rel" '$2==name {found=1} END {exit !found}' "$PKG/manifest.sha256" || exit 1
    done || fail '安装包有未经校验的文件'
    [ "$(cat "$PKG/version")" = 3.0.0 ] || fail '插件版本无效'
}

platform() {
    [ -d "$KSROOT" ] && which dbus >/dev/null 2>&1 && which nvram >/dev/null 2>&1 || fail '未找到软件中心'
    if [ "$KSROOT" = /koolshare ]; then [ -x /usr/bin/skipd ] || fail '软件中心服务不可用'; fi
    uname -r | awk -F. '$1>4 || ($1==4 && $2>=1) {ok=1} END {exit !ok}' || fail '需要 Linux 4.1 或更新的固件'
    MODEL=$(nvram get odmpid 2>/dev/null)
    [ -n "$MODEL" ] || MODEL=$(nvram get productid 2>/dev/null)
    # Same model/platform mapping as Koolshare ks_tar_install.sh.
    case "$MODEL" in
        TX-AX6000|TUF-AX4200Q|RT-AX57_Go|GS7|ZenWiFi_BT8P|GS7_Air|GS-BE7200X) PLATFORM=mtk; ARCH=arm64;;
        ZenWiFi_BD4) PLATFORM=ipq32; ARCH=arm;;
        TUF_6500) PLATFORM=ipq64; ARCH=arm64;;
        RT-AX89X) PLATFORM=qca; ARCH=arm;;
        *) PLATFORM=hnd; ARCH=arm;;
    esac
    grep -qx "$PLATFORM" "$PKG/.valid" || fail '安装包与固件平台不匹配'
    PAYLOAD=$PKG/payload/$ARCH
    [ -x "$PAYLOAD/tsks-helper" ] && [ -f "$PAYLOAD/tailscale.combined" ] || fail '安装包缺少本机架构的内核'
    VERIFIED_HELPER=$PAYLOAD/tsks-helper
}

space_preflight() {
    local path rel size total=0 available memory
    # Copies stay on JFFS. Count only selected payload, regular-file backups,
    # and the adopted/fresh core; retain 8 MiB beyond the full transaction.
    for path in "$PKG"/scripts/tailscale_* "$PKG"/init.d/*tailscale.sh "$PKG"/webs/Module_tailscale.asp "$PKG"/res/icon-tailscale.png "$PKG"/res/tailscale3.js "$PKG"/res/LICENSE*; do
        [ -f "$path" ] || continue
        size=$(wc -c <"$path" | tr -d ' '); total=$((total + size + 4096))
        rel=${path#"$PKG/"}
        if [ -f "$KSROOT/$rel" ] && [ ! -L "$KSROOT/$rel" ]; then
            size=$(wc -c <"$KSROOT/$rel" | tr -d ' '); total=$((total + size + 4096))
        fi
    done
    for path in "$PAYLOAD/tsks-helper" "$PKG/uninstall.sh" "$PKG/release.pub"; do
        size=$(wc -c <"$path" | tr -d ' '); total=$((total + size + 4096))
    done
    for path in "$KSROOT/bin/tsks-helper" "$KSROOT/bin/tailscale" "$KSROOT/bin/tailscaled" "$KSROOT/bin/tailscale.combined" "$KSROOT/scripts/uninstall_tailscale.sh" "$DATA/release.pub" "$STATE"; do
        if [ -f "$path" ] && [ ! -L "$path" ]; then
            size=$(wc -c <"$path" | tr -d ' '); total=$((total + size + 4096))
        fi
    done
    if [ ! -L "$DATA/current" ]; then
        path=$PAYLOAD/tailscale.combined
        [ ! -f "$KSROOT/bin/tailscale.combined" ] || path=$KSROOT/bin/tailscale.combined
        size=$(wc -c <"$path" | tr -d ' '); total=$((total + size + 4096))
    fi
    available=$(df -Pk "$KSROOT" | awk 'NR>1 {space=$4} END {print space}')
    case $available in ''|*[!0-9]*) fail '无法读取剩余存储空间';; esac
    [ "$available" -ge "$(( (total + 8388608 + 1023) / 1024 ))" ] || fail '可用存储不足：需要安装暂存空间及 8 MiB 余量'
    memory=$(awk '$1=="MemAvailable:" {available=$2} $1=="MemFree:" {free=$2} $1=="Buffers:" {buffers=$2} $1=="Cached:" {cached=$2} END {if(available>0)print available;else print free+buffers+cached}' "${TSKS_PROC:-/proc}/meminfo" 2>/dev/null)
    case $memory in ''|*[!0-9]*) fail '无法读取可用内存';; esac
    [ "$memory" -ge 32768 ] || fail '可用内存不足：安装至少需要 32 MiB'
}

elf_ok() {
    # ELF magic, class, little-endian and e_machine. Never execute wrong-arch data.
    local file=$1 arch=$2 signature machine
    signature=$(od -An -tu1 -N6 "$file" | tr -s ' ' | sed 's/^ //;s/ $//')
    machine=$(od -An -tu1 -j18 -N2 "$file" | tr -s ' ' | sed 's/^ //;s/ $//')
    case $arch in arm) [ "$signature" = '127 69 76 70 1 1' ] && [ "$machine" = '40 0' ];;
        arm64) [ "$signature" = '127 69 76 70 2 1' ] && [ "$machine" = '183 0' ];; *) return 1;; esac
}

validate_core() {
    local binary=$1 descriptor=$2 expected size version actual
    [ -f "$binary" ] && [ ! -L "$binary" ] && elf_ok "$binary" "$ARCH" || return 1
    [ "$("$VERIFIED_HELPER" json-get "$descriptor" arch)" = "$ARCH" ] || return 1
    expected=$("$VERIFIED_HELPER" json-get "$descriptor" binary_sha256) || return 1
    size=$("$VERIFIED_HELPER" json-get "$descriptor" unpacked_size) || return 1
    version=$("$VERIFIED_HELPER" json-get "$descriptor" version) || return 1
    [ "$(sha256sum "$binary" | awk '{print $1}')" = "$expected" ] && [ "$(wc -c <"$binary" | tr -d ' ')" = "$size" ] || return 1
    [ "$size" -le 12582912 ] && [ "$size" -gt 64 ] || return 1
    actual=$("$VERIFIED_HELPER" version "$binary") || return 1
    [ "$actual" = "$version" ]
}

core_link_ok() {
    case $1 in cores/*) ;; *) return 1;; esac
    case $1 in *[!A-Za-z0-9./_-]*|*..*|*//*|cores/) return 1;; esac
    [ "${1#cores/}" = "${1##*/}" ] && [ -d "$DATA/$1" ] && [ ! -L "$DATA/$1" ]
}

prepare_core() {
    [ ! -L "$DATA" ] && [ ! -L "$DATA/cores" ] || fail '内核目录不能使用外部符号链接'
    "$VERIFIED_HELPER" verify "$PKG/release.json" "$PKG/release.pub" "$ARCH" >"$STAGE/verified.json" || fail '内核签名校验失败'
    validate_core "$PAYLOAD/tailscale.combined" "$STAGE/verified.json" || fail '内核文件与签名不一致'
    CORE_VERSION=$("$VERIFIED_HELPER" json-get "$STAGE/verified.json" version) || fail '无效内核版本'
    CORE_BUILD=$("$VERIFIED_HELPER" json-get "$STAGE/verified.json" build) || fail '无效内核构建版本'
    if [ -L "$DATA/current" ]; then
        OLD_CORE=$(readlink "$DATA/current")
        core_link_ok "$OLD_CORE" && validate_core "$DATA/$OLD_CORE/tailscale.combined" "$DATA/$OLD_CORE/descriptor.json" || fail '已安装内核校验失败，保留现状'
        [ "$(readlink "$DATA/$OLD_CORE/tailscale")" = tailscale.combined ] && [ "$(readlink "$DATA/$OLD_CORE/tailscaled")" = tailscale.combined ] || fail '已安装内核链接无效'
        OLD_KIND=current
        SELECTED=$OLD_CORE
        say '保留已安装内核；可在插件页面手动检查更新。'
    elif [ -e "$DATA/current" ]; then
        fail '现有内核目录结构无法安全迁移'
    elif [ -f "$KSROOT/bin/tailscale.combined" ] && [ ! -L "$KSROOT/bin/tailscale.combined" ]; then
        elf_ok "$KSROOT/bin/tailscale.combined" "$ARCH" || fail '旧内核架构与平台不匹配'
        LEGACY_VERSION=$("$VERIFIED_HELPER" version "$KSROOT/bin/tailscale.combined") || fail '无法读取旧内核版本'
        LEGACY_SIZE=$(wc -c <"$KSROOT/bin/tailscale.combined" | tr -d ' ')
        [ "$LEGACY_SIZE" -le 12582912 ] && [ "$LEGACY_SIZE" -gt 64 ] || fail '旧内核大小不受支持'
        LEGACY_SHA=$(sha256sum "$KSROOT/bin/tailscale.combined" | awk '{print $1}')
        SELECTED=cores/$LEGACY_VERSION-legacy-$ARCH
        mkdir -p "$STAGE/core" || fail '无法准备内核目录'
        cp -p "$KSROOT/bin/tailscale.combined" "$STAGE/core/tailscale.combined" || fail '无法保留旧内核'
        printf '{"schema":1,"version":"%s","build":"legacy","arch":"%s","origin":"legacy","unpacked_size":%s,"binary_sha256":"%s"}\n' "$LEGACY_VERSION" "$ARCH" "$LEGACY_SIZE" "$LEGACY_SHA" >"$STAGE/core/descriptor.json"
        OLD_KIND=legacy
        say '保留原 Tailscale 内核及身份；升级插件后可手动更新内核。'
    else
        SELECTED=cores/$CORE_VERSION-$CORE_BUILD-$ARCH
        mkdir -p "$STAGE/core" || fail '无法准备内核目录'
        cp -p "$PAYLOAD/tailscale.combined" "$STAGE/core/tailscale.combined" && cp "$STAGE/verified.json" "$STAGE/core/descriptor.json" || fail '无法准备内核'
    fi
    if [ "$OLD_KIND" != current ]; then
        ln -s tailscale.combined "$STAGE/core/tailscale" && ln -s tailscale.combined "$STAGE/core/tailscaled" || fail '无法创建内核链接'
        chmod 755 "$STAGE/core/tailscale.combined"
        if [ -e "$DATA/$SELECTED" ]; then
            [ ! -L "$DATA/$SELECTED" ] && validate_core "$DATA/$SELECTED/tailscale.combined" "$STAGE/core/descriptor.json" || fail '现有内核版本目录冲突'
        fi
    fi
}

prepare_files() {
    local path rel
    mkdir -p "$STAGE/new" "$STAGE/backup" "$STAGE/dbus" || fail '无法准备安装事务'
    : >"$STAGE/files"
    for path in "$PKG"/scripts/tailscale_* "$PKG"/init.d/*tailscale.sh "$PKG"/webs/Module_tailscale.asp "$PKG"/res/icon-tailscale.png "$PKG"/res/tailscale3.js; do
        [ -f "$path" ] || fail '安装包缺少插件文件'
        rel=${path#"$PKG/"}
        mkdir -p "$STAGE/new/${rel%/*}" || fail '无法准备文件目录'
        cp -p "$path" "$STAGE/new/$rel" || fail '无法准备插件文件'
        printf '%s\n' "$rel" >>"$STAGE/files"
    done
    for path in "$PKG"/res/LICENSE*; do
        [ -f "$path" ] || continue
        rel=${path#"$PKG/"}
        cp -p "$path" "$STAGE/new/$rel" || fail '无法准备许可声明'
        printf '%s\n' "$rel" >>"$STAGE/files"
    done
    mkdir -p "$STAGE/new/bin" "$STAGE/new/tailscale" || fail '无法准备安装目录'
    cp -p "$PAYLOAD/tsks-helper" "$STAGE/new/bin/tsks-helper" &&
        cp -p "$PKG/uninstall.sh" "$STAGE/new/scripts/uninstall_tailscale.sh" &&
        cp "$PKG/release.pub" "$STAGE/new/tailscale/release.pub" || fail '无法准备管理文件'
    ln -s ../tailscale/current/tailscale "$STAGE/new/bin/tailscale" && ln -s ../tailscale/current/tailscaled "$STAGE/new/bin/tailscaled" || fail '无法准备命令链接'
    printf '%s\n' bin/tsks-helper bin/tailscale bin/tailscaled bin/tailscale.combined scripts/uninstall_tailscale.sh tailscale/release.pub tailscale/current >>"$STAGE/files"
    chmod 755 "$STAGE/new/bin/tsks-helper" "$STAGE/new/scripts/"* "$STAGE/new/init.d/"*
    chmod 644 "$STAGE/new/webs/"* "$STAGE/new/res/"* "$STAGE/new/tailscale/release.pub"
    while IFS= read -r rel; do
        path=$KSROOT/$rel
        [ ! -d "$path" ] || [ -L "$path" ] || fail '目标文件被目录占用'
        if [ -e "$path" ] || [ -L "$path" ]; then
            mkdir -p "$STAGE/backup/${rel%/*}" && cp -a "$path" "$STAGE/backup/$rel" || fail '无法备份现有文件'
        fi
    done <"$STAGE/files"
    for key in tailscale_enable tailscale_ipv4_enable tailscale_ipv6_enable tailscale_advertise_routes tailscale_accept_routes tailscale_exit_node tailscale_watchdog_enable tailscale_version softcenter_module_tailscale_version softcenter_module_tailscale_install softcenter_module_tailscale_name softcenter_module_tailscale_title softcenter_module_tailscale_description; do
        dbus get "$key" >"$STAGE/dbus/$key" || fail '无法备份插件设置'
    done
}

legacy_pid_matches() {
    local cmd=$1
    [ -r "$cmd" ] || return 1
    tr '\000' '\n' <"$cmd" | awk -v state="$STATE" -v binary="$KSROOT/bin/tailscaled" -v combined="$KSROOT/bin/tailscale.combined" '
        $0==binary || $0==combined || $0=="tailscaled" {exe=1}
        $0=="--state="state || $0=="-state="state || ((prev=="--state" || prev=="-state") && $0==state) {matched=1}
        {prev=$0} END {exit !(exe&&matched)}'
}

remember_port() {
    case $1 in ''|*[!0-9]*) return;; esac
    [ "${#1}" -le 5 ] && [ "$1" -le 65535 ] || return
    OLD_PORT=$1
}

stop_legacy() {
    local file pid tries value previous
    for file in "${TSKS_PROC:-/proc}"/[0-9]*/cmdline; do
        legacy_pid_matches "$file" || continue
        pid=${file%/cmdline}; pid=${pid##*/}
        case $pid in ''|*[!0-9]*) continue;; esac
        [ "$pid" -gt 1 ] || continue
        # Recover only the socket argument, never evaluate the original argv.
        previous=
        while IFS= read -r value; do
            case $value in --socket=/*|-socket=/*) OLD_SOCKET=${value#*=};; esac
            if [ "$previous" = --socket ] || [ "$previous" = -socket ]; then case $value in /*) OLD_SOCKET=$value;; esac; fi
            case $value in --port=*|-port=*) remember_port "${value#*=}";; esac
            if [ "$previous" = --port ] || [ "$previous" = -port ]; then remember_port "$value"; fi
            previous=$value
        done <<ARGS
$(tr '\000' '\n' <"$file")
ARGS
        legacy_pid_matches "$file" || continue
        kill "$pid" 2>/dev/null || continue
        tries=0
        while kill -0 "$pid" 2>/dev/null && [ "$tries" -lt 10 ]; do sleep 1; tries=$((tries + 1)); done
        if kill -0 "$pid" 2>/dev/null && legacy_pid_matches "$file"; then
            kill -KILL "$pid" 2>/dev/null || return 1
            sleep 1
            if kill -0 "$pid" 2>/dev/null && legacy_pid_matches "$file"; then return 1; fi
        fi
    done
}

restore() {
    local rel key value tries rc=0
    say '正在恢复安装前的文件、设置和连接身份。' >&2
    HELPER=$VERIFIED_HELPER
    ts_stop >/dev/null 2>&1 || :
    if [ "$TOUCHED" = 1 ]; then
        while IFS= read -r rel; do
            rm -f "$KSROOT/$rel" || rc=1
            if [ -e "$STAGE/backup/$rel" ] || [ -L "$STAGE/backup/$rel" ]; then
                cp -a "$STAGE/backup/$rel" "$KSROOT/$rel" || rc=1
            fi
        done <"$STAGE/files"
        [ -z "$NEW_CORE" ] || rm -rf "$DATA/$NEW_CORE"
        for key in "$STAGE"/dbus/*; do
            value=$(cat "$key")
            if [ -n "$value" ]; then dbus set "${key##*/}=$value" || rc=1; else dbus remove "${key##*/}" || rc=1; fi
        done
    fi
    if [ -f "$STAGE/state.present" ]; then
        cp -p "$STAGE/state" "$STATE" || rc=1
    fi
    if [ "$(dbus get tailscale_enable)" = 1 ]; then
        if [ "$OLD_KIND" = current ]; then
            HELPER=$VERIFIED_HELPER
            ts_start >/dev/null 2>&1 || rc=1
        elif [ "$OLD_KIND" = legacy ] && [ -x "$KSROOT/bin/tailscaled" ]; then
            set -- "$KSROOT/bin/tailscaled" "--state=$STATE" "--socket=$OLD_SOCKET"
            [ -z "$OLD_PORT" ] || set -- "$@" "--port=$OLD_PORT"
            "$@" </dev/null >/dev/null 2>&1 9>&- &
            tries=0
            while [ "$tries" -lt 15 ]; do
                if "$VERIFIED_HELPER" status "$OLD_SOCKET" >"$STAGE/restore-status.json" 2>/dev/null && [ "$("$VERIFIED_HELPER" json-get "$STAGE/restore-status.json" ok)" = true ]; then break; fi
                sleep 1; tries=$((tries + 1))
            done
            [ "$tries" -lt 15 ] || rc=1
            SOCKET=$OLD_SOCKET
            ts_firewall_apply >/dev/null 2>&1 || rc=1
        fi
    fi
    [ "$rc" = 0 ] || say "自动恢复未全部完成；安装备份保留于 $STAGE" >&2
    return "$rc"
}

cleanup() {
    local rc=$?
    trap - EXIT HUP INT TERM
    if [ "$COMMITTED" != 1 ] && [ "$STOPPED" = 1 ]; then restore || { rc=1; KEEP_STAGE=1; }; fi
    if [ "$LOCKED" = 1 ]; then flock -u 9; exec 9>&-; fi
    [ "${KEEP_STAGE:-0}" = 1 ] || rm -rf "$STAGE"
    exit "$rc"
}

check_package
platform
mkdir -p "$STAGE" "$RUN" || fail '无法创建安全暂存目录'
chmod 700 "$STAGE" "$RUN"
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
# Package files have been checksum-validated before sourcing the library.
. "$PKG/scripts/tailscale_lib.sh"
ts_init || fail '无法初始化插件运行目录'
HELPER=$VERIFIED_HELPER
ts_lock || fail '另一个操作正在运行，请稍后安装'
LOCKED=1
# An unfinished core transaction must be recovered before plugin replacement.
[ ! -e "$DATA/update.txn" ] && [ ! -L "$DATA/update.txn" ] || fail 'recovery_required：上次内核操作尚未恢复，请先完成内核恢复后再安装'
# All version probes, snapshots and replacements share the lifecycle lock.
space_preflight
prepare_core
prepare_files
STOPPED=1
stop_legacy && ts_stop || fail '无法安全停止现有服务'
if [ -f "$STATE" ]; then cp -p "$STATE" "$STAGE/state" && : >"$STAGE/state.present" || fail '无法备份连接身份'; else : >"$STAGE/state.absent"; fi
TOUCHED=1
mkdir -p "$DATA/cores" "$KSROOT/bin" "$KSROOT/scripts" "$KSROOT/init.d" "$KSROOT/webs" "$KSROOT/res" || fail '无法创建插件目录'
if [ "$OLD_KIND" != current ] && [ ! -e "$DATA/$SELECTED" ]; then
    mv "$STAGE/core" "$DATA/$SELECTED" || fail '无法安装内核'
    NEW_CORE=$SELECTED
fi
while IFS= read -r rel; do
    [ "$rel" = tailscale/current ] && continue
    if [ -e "$STAGE/new/$rel" ] || [ -L "$STAGE/new/$rel" ]; then
        mv -f "$STAGE/new/$rel" "$KSROOT/$rel" || fail '无法替换插件文件'
    elif [ "$rel" = bin/tailscale.combined ]; then
        rm -f "$KSROOT/$rel" || fail '无法迁移旧内核文件'
    fi
done <"$STAGE/files"
"$VERIFIED_HELPER" atomic-link "$SELECTED" "$DATA/current" || fail '无法切换内核链接'
for pair in tailscale_enable:0 tailscale_ipv4_enable:1 tailscale_ipv6_enable:1 tailscale_advertise_routes:1 tailscale_accept_routes:1 tailscale_exit_node:0 tailscale_watchdog_enable:1; do
    key=${pair%:*}; value=${pair#*:}
    [ -n "$(dbus get "$key")" ] || dbus set "$key=$value" || fail '无法设置默认参数'
done
HELPER=$KSROOT/bin/tsks-helper
ts_start || fail '服务就绪检查失败'
# Registration follows successful readiness, including disabled fresh installs.
for pair in tailscale_version=3.0.0 softcenter_module_tailscale_version=3.0.0 softcenter_module_tailscale_install=1 softcenter_module_tailscale_name=tailscale softcenter_module_tailscale_title=Tailscale 'softcenter_module_tailscale_description=安全组网、内核更新与自动恢复'; do
    dbus set "$pair" || fail '无法注册插件'
done
COMMITTED=1
say "Tailscale 3.0.0 安装完成（${PLATFORM} / ${ARCH}）；现有配置与连接身份已保留。"
