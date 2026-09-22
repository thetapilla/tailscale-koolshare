#!/bin/sh
# BusyBox ash 1.25 shared backend. Source, call ts_init, then ts_lock once.
# Public lifecycle: ts_start, ts_stop, ts_restart, ts_firewall_apply/remove.
# Public jobs: ts_job_begin ID; ts_job_write STATE PHASE MESSAGE;
# ts_job_log MESSAGE; ts_reply JSON; ts_unlock. Lifecycle never retakes lock.
# DATA/current and DATA/previous are managed exclusively by the core updater.

ts_init() {
    KSROOT=${TSKS_ROOT:-/koolshare}
    PATH="$KSROOT/bin:$KSROOT/scripts:${PATH:-/usr/sbin:/usr/bin:/sbin:/bin}"
    export PATH
    DATA=$KSROOT/tailscale
    STATE=$KSROOT/configs/tailscale/tailscaled.state
    RUN=${TSKS_RUN:-/tmp/tailscale3}
    WEB=${TSKS_WEB:-/tmp/upload}
    HELPER=$KSROOT/bin/tsks-helper
    SOCKET=$RUN/tailscaled.sock
    TSKS_SYSFS=${TSKS_SYSFS:-/sys}
    TSKS_PROC=${TSKS_PROC:-/proc}
    export KSROOT DATA STATE RUN WEB HELPER SOCKET
    umask 077
    mkdir -p "$RUN" "${STATE%/*}" || return 1
    chmod 700 "$RUN" "${STATE%/*}" || return 1
    [ ! -d /tmp/.xt ] || export XTABLES_LIBDIR=/tmp/.xt
    TS_LOCKED=0
    TS_IPTABLES_WAIT=
    TS_IP6TABLES_WAIT=
}

ts_now() { date +%s; }
ts_uint() { case ${1:-} in ''|*[!0-9]*|0[0-9]*) return 1;; esac; [ "${#1}" -le 12 ]; }
ts_counter() { case ${1:-} in ''|*[!0-9]*|0[0-9]*) return 1;; esac; [ "${#1}" -le 20 ]; }
ts_job_id() { case ${1:-} in ''|*[!0-9]*) return 1;; esac; [ "${#1}" -le 15 ]; }
ts_quote() { "$HELPER" quote "$1"; }
ts_get() { "$HELPER" json-get "$1" "$2" 2>/dev/null; }
ts_bool() { [ "$1" = 1 ] && printf true || printf false; }
ts_bound() { "$HELPER" timeout "$@"; }
ts_temp() { "$HELPER" temp "$1"; }

ts_ipt() {
    local binary=$1 wait help
    shift
    case $binary in
        iptables) wait=$TS_IPTABLES_WAIT;;
        ip6tables) wait=$TS_IP6TABLES_WAIT;;
        *) return 1;;
    esac
    if [ -z "$wait" ]; then
        # Older firmware has no xtables wait option. Read advertised options;
        # never probe compatibility by changing a rule.
        wait=failed
        if help=$(ts_bound 3 "$binary" --help 2>&1); then
            wait=no
            if printf '%s\n' "$help" | grep -Eq -- '(^|[[:space:],])(-w|--wait)([[:space:],]|$)'; then wait=yes; fi
        fi
        case $binary in iptables) TS_IPTABLES_WAIT=$wait;; ip6tables) TS_IP6TABLES_WAIT=$wait;; esac
    fi
    [ "$wait" != failed ] || return 1
    # Bound legacy commands and versions whose -w has no seconds argument.
    [ "$wait" != yes ] || set -- -w "$@"
    ts_bound 5 "$binary" "$@"
}

ts_lock() {
    [ "$TS_LOCKED" = 1 ] && return 0
    exec 9>"$RUN/operation.lock" || return 1
    flock -n 9 || { exec 9>&-; return 1; }
    TS_LOCKED=1
}
ts_unlock() {
    [ "$TS_LOCKED" = 1 ] || return 0
    flock -u 9
    exec 9>&-
    TS_LOCKED=0
}

ts_reply() {
    if [ -n "${ID:-}" ]; then
        ts_job_id "$ID" || return 1
        # base.sh's response transport, without importing dbus via eval.
        curl --noproxy '*' -fsS --connect-timeout 2 --max-time 5 \
                -X POST -d "$1" "http://127.0.0.1:3030/_resp/$ID" >/dev/null 2>&1
    else
        printf '%s\n' "$1"
    fi
}

ts_job_begin() {
    ts_job_id "$1" || return 1
    TS_JOB=$1
    mkdir -p "$WEB" || return 1
    # Only sanitized progress goes here. Keep at most 24 public jobs.
    local old
    for old in $(ls -t "$WEB"/tailscale3_*.json 2>/dev/null | tail -n +25); do
        case ${old##*/} in tailscale3_*.json) rm -f "$old" "${old%.json}.log";; esac
    done
    local target="$WEB/tailscale3_$TS_JOB.log" tmp
    ts_public_file "$target" && ts_public_file "$WEB/tailscale3_$TS_JOB.json" || return 1
    tmp=$(ts_temp "$WEB/.tailscale3.XXXXXX") || return 1
    chmod 644 "$tmp" && mv -f "$tmp" "$target" || { rm -f "$tmp"; return 1; }
    ts_job_write running accepted '已接收操作请求'
}

ts_public_file() { [ ! -L "$1" ] && { [ ! -e "$1" ] || [ -f "$1" ]; }; }

ts_job_write() {
    [ -n "${TS_JOB:-}" ] || return 0
    case $1 in running|success|failed|rolled_back) ;; *) return 1;; esac
    local target="$WEB/tailscale3_$TS_JOB.json" tmp
    ts_public_file "$target" || return 1
    tmp=$(ts_temp "$WEB/.tailscale3.XXXXXX") || return 1
    printf '{"schema":1,"id":"%s","state":%s,"phase":%s,"message":%s,"updated_at":%s}\n' \
        "$TS_JOB" "$(ts_quote "$1")" "$(ts_quote "$2")" "$(ts_quote "$3")" "$(ts_now)" >"$tmp" || { rm -f "$tmp"; return 1; }
    chmod 644 "$tmp" && mv -f "$tmp" "$target" || { rm -f "$tmp"; return 1; }
}

ts_job_log() {
    # Callers pass generated safe messages, never CLI output, state or secrets.
    local line="$(date -u '+%Y-%m-%dT%H:%M:%SZ') $1" target
    target="$RUN/events.log"
    printf '%s\n' "$line" >>"$target"
    if [ "$(wc -c <"$target")" -gt 32768 ]; then
        tail -c 16384 "$target" >"$target.new" && mv -f "$target.new" "$target"
    fi
    if [ -n "${TS_JOB:-}" ]; then
        target="$WEB/tailscale3_$TS_JOB.log"
        ts_public_file "$target" || return 1
        local tmp
        tmp=$(ts_temp "$WEB/.tailscale3.XXXXXX") || return 1
        [ ! -f "$target" ] || tail -c 16384 "$target" >"$tmp"
        printf '%s\n' "$line" >>"$tmp"
        chmod 644 "$tmp" && mv -f "$tmp" "$target" || { rm -f "$tmp"; return 1; }
    fi
}

ts_read_bool() {
    local value
    value=$(dbus get "$1" 2>/dev/null) || value=
    case $value in 0|1) printf '%s' "$value";; '') printf '%s' "$2";; *) return 1;; esac
}

ts_config_read() {
    ENABLE=$(ts_read_bool tailscale_enable 0) &&
    IPV4=$(ts_read_bool tailscale_ipv4_enable 1) &&
    IPV6=$(ts_read_bool tailscale_ipv6_enable 1) &&
    ADVERTISE=$(ts_read_bool tailscale_advertise_routes 1) &&
    ACCEPT=$(ts_read_bool tailscale_accept_routes 1) &&
    EXIT_NODE=$(ts_read_bool tailscale_exit_node 0) &&
    WATCHDOG=$(ts_read_bool tailscale_watchdog_enable 1)
}

ts_bits_valid() { [ "${#1}" = 7 ] && { case $1 in *[!01]*) return 1;; esac; }; }

ts_config_keys() {
    printf '%s\n' tailscale_enable tailscale_ipv4_enable tailscale_ipv6_enable \
        tailscale_advertise_routes tailscale_accept_routes tailscale_exit_node tailscale_watchdog_enable
}

ts_config_snapshot() {
    local key value
    : >"$RUN/config.previous"
    for key in $(ts_config_keys); do
        value=$(dbus get "$key" 2>/dev/null) || return 1
        case $value in ''|0|1) ;; *) return 1;; esac
        printf '%s=%s\n' "$key" "$value" >>"$RUN/config.previous" || return 1
    done
}

ts_config_bits_apply() {
    local remaining=$1 key bit
    [ "$TS_LOCKED" = 1 ] && ts_bits_valid "$remaining" || return 1
    for key in $(ts_config_keys); do
        bit=${remaining%"${remaining#?}"}; remaining=${remaining#?}
        dbus set "$key=$bit" >/dev/null 2>&1 || return 1
    done
}

ts_config_restore() {
    local key value failed=0
    [ "$TS_LOCKED" = 1 ] && [ -f "$RUN/config.previous" ] || return 1
    while IFS='=' read -r key value; do
        case $key in tailscale_enable|tailscale_ipv4_enable|tailscale_ipv6_enable|tailscale_advertise_routes|tailscale_accept_routes|tailscale_exit_node|tailscale_watchdog_enable) ;; *) return 1;; esac
        case $value in 0|1) dbus set "$key=$value" >/dev/null 2>&1 || failed=1;;
            '') dbus remove "$key" >/dev/null 2>&1 || failed=1;; *) return 1;; esac
    done <"$RUN/config.previous"
    return "$failed"
}

ts_ipv4() {
    printf '%s\n' "$1" | awk -F. 'NF!=4 {exit 1} {for(i=1;i<=4;i++) if($i!~/^[0-9]+$/ || length($i)>3 || $i+0>255) exit 1}'
}

ts_lan() {
    LAN_IF=$(nvram get lan_ifname 2>/dev/null)
    [ -n "$LAN_IF" ] || LAN_IF=br0
    case $LAN_IF in *[!A-Za-z0-9_.:-]*|-*|'') return 1;; esac
    [ "${#LAN_IF}" -le 15 ] || return 1
    LAN_IP=$(nvram get lan_ipaddr 2>/dev/null)
    local mask
    mask=$(nvram get lan_netmask 2>/dev/null)
    ts_ipv4 "$LAN_IP" && ts_ipv4 "$mask" || return 1
    LAN_CIDR=$(awk -v ip="$LAN_IP" -v mask="$mask" 'BEGIN {
        split(ip,a,".");split(mask,m,".");prefix=0;zero=0;net="";
        for(i=1;i<=4;i++){n=0;for(b=128;b>=1;b/=2){bit=int(m[i]/b)%2;
            if(bit){if(zero)exit 1;prefix++;if(int(a[i]/b)%2)n+=b}else zero=1}
            net=sprintf("%s%s%d",net,(i==1?"":"."),n)}
        if(prefix<1 || prefix>32)exit 1;print net "/" prefix
    }') && [ -n "$LAN_CIDR" ]
}

ts_status_file() {
    local target=$1
    if ! "$HELPER" status "$SOCKET" >"$target.tmp" 2>/dev/null; then
        rm -f "$target.tmp"
        return 1
    fi
    mv -f "$target.tmp" "$target"
}

ts_cli() { ts_bound 15 "$DATA/current/tailscale" --socket="$SOCKET" "$@"; }

ts_pid_matches() {
    # Only the exact installed executable and socket argument establish daemon
    # ownership. A stale PID must never target an unrelated process.
    local pid args
    pid=$1
    ts_uint "$pid" && [ "$pid" -gt 1 ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    [ -r "$TSKS_PROC/$pid/cmdline" ] || return 1
    args=$(tr '\000' ' ' <"$TSKS_PROC/$pid/cmdline")
    case " $args " in *" --socket=$SOCKET "*) ;; *) return 1;; esac
    case " $args " in *" $DATA/current/tailscaled "*|*" $DATA/current/tailscale.combined "*) return 0;; *) return 1;; esac
}

ts_pid_alive() {
    local pid file count=0
    pid=$(cat "$RUN/tailscaled.pid" 2>/dev/null)
    ts_pid_matches "$pid" && return 0
    # Recover a lost RAM pid file without spawning a second daemon. Router
    # process counts are small; cap this scan so corrupted fixtures cannot turn
    # a watchdog invocation into an unbounded traversal.
    for file in "$TSKS_PROC"/[0-9]*/cmdline; do
        [ -r "$file" ] || continue
        count=$((count + 1))
        [ "$count" -le 4096 ] || break
        pid=${file%/cmdline}; pid=${pid##*/}
        if ts_pid_matches "$pid"; then
            printf '%s\n' "$pid" >"$RUN/tailscaled.pid"
            return 0
        fi
    done
    return 1
}

ts_cron() {
    which cru >/dev/null 2>&1 || return 0
    cru d tailscale_watchdog >/dev/null 2>&1
    # The minute task also finishes address-dependent network configuration
    # after authorization. The watchdog flag controls automatic recovery only.
    if [ "$ENABLE" = 1 ]; then
        cru a tailscale_watchdog "* * * * * $KSROOT/scripts/tailscale_watchdog" >/dev/null 2>&1
    fi
}

ts_legacy_cleanup() {
    # Restrict migration to known historical targets and the exact tag. Never
    # delete external Fullcone hooks, even when somebody reused this comment.
    local binary=$1 table line token bad
    which "${binary}-save" >/dev/null 2>&1 || return 0
    for table in filter nat; do
        ts_bound 5 "${binary}-save" -t "$table" 2>/dev/null | awk '
            /^-A / && /--comment ("tailscale_rule"|tailscale_rule)( |$)/ && /-j (ACCEPT|DROP|MASQUERADE|DNAT)( |$)/ {
                gsub(/"tailscale_rule"/,"tailscale_rule");sub(/^-A /,"-D ");print
            }' >"$RUN/legacy-rules"
        while IFS= read -r line; do
            bad=0
            # Word splitting is deliberate; expansion is never re-evaluated.
            # Reject quoted/escaped/complex rules whose exact argv is unclear.
            case $line in *[!A-Za-z0-9_.,:/!@%+=\ -]*) bad=1;; esac
            [ "$bad" = 0 ] || continue
            set -f
            set -- $line
            set +f
            ts_ipt "$binary" -t "$table" "$@" >/dev/null 2>&1 || :
        done <"$RUN/legacy-rules"
    done
    rm -f "$RUN/legacy-rules"
}

ts_chain() {
    local binary=$1 table=$2 parent=$3 chain=$4
    ts_ipt "$binary" -t "$table" -N "$chain" 2>/dev/null || :
    ts_ipt "$binary" -t "$table" -F "$chain" || return 1
    if [ "$table:$parent" = nat:POSTROUTING ]; then
        # Fullcone and other established NAT hooks get first opportunity;
        # plugin MASQUERADE is the final fallback for LAN-to-tailnet traffic.
        while ts_ipt "$binary" -t "$table" -D "$parent" -j "$chain" 2>/dev/null; do :; done
        ts_ipt "$binary" -t "$table" -A "$parent" -j "$chain"
    elif ! ts_ipt "$binary" -t "$table" -C "$parent" -j "$chain" 2>/dev/null; then
        ts_ipt "$binary" -t "$table" -I "$parent" 1 -j "$chain"
    fi
}

ts_firewall_apply() {
    ts_config_read || return 1
    [ "$ENABLE" = 1 ] || { ts_firewall_remove; return; }
    local binary allowed tailip index status_source=${1:-}
    # Consume only events that precede this refresh. A concurrent NAT event
    # leaves a new marker for the next minute task while fd9 is held here.
    rm -f "$RUN/nat-pending"
    # Only a fully successful apply may establish the address cache. A failed
    # refresh must be retried by maintenance even when addresses stay the same.
    rm -f "$RUN/firewall-status.json"
    for binary in iptables ip6tables; do
        if ! which "$binary" >/dev/null 2>&1; then
            [ "$binary" = ip6tables ] && continue
            return 1
        fi
        ts_legacy_cleanup "$binary"
        ts_chain "$binary" filter INPUT TSKS_INPUT &&
        ts_chain "$binary" filter OUTPUT TSKS_OUTPUT &&
        ts_chain "$binary" filter FORWARD TSKS_FORWARD || return 1
        allowed=$IPV4
        [ "$binary" != ip6tables ] || allowed=$IPV6
        if [ "$allowed" = 0 ]; then
            ts_ipt "$binary" -A TSKS_INPUT -p udp --dport 41641 -j DROP &&
            ts_ipt "$binary" -A TSKS_OUTPUT -p udp --sport 41641 -j DROP || return 1
        fi
        if ts_lan; then
            ts_ipt "$binary" -A TSKS_FORWARD -i tailscale0 -o "$LAN_IF" -j ACCEPT &&
            ts_ipt "$binary" -A TSKS_FORWARD -i "$LAN_IF" -o tailscale0 -j ACCEPT || return 1
        fi
    done
    # Tailscale's netfilter remains enabled. This additional NAT is specifically
    # for LAN clients reaching remote tailnet/subnet destinations.
    if ts_lan; then
        ts_chain iptables nat POSTROUTING TSKS_POSTROUTING &&
        ts_chain iptables nat PREROUTING TSKS_PREROUTING || return 1
        ts_ipt iptables -t nat -A TSKS_POSTROUTING -s "$LAN_CIDR" -o tailscale0 -j MASQUERADE || return 1
        if [ -n "$status_source" ]; then
            cp "$status_source" "$RUN/firewall-next.json" || return 1
        else
            ts_status_file "$RUN/firewall-next.json" || return 0
        fi
        if [ -f "$RUN/firewall-next.json" ]; then
            index=0
            while [ "$index" -lt 8 ]; do
                tailip=$(ts_get "$RUN/firewall-next.json" "ips.$index") || break
                if ts_ipv4 "$tailip"; then
                    ts_ipt iptables -t nat -A TSKS_PREROUTING -i tailscale0 -d "$tailip/32" -j DNAT --to-destination "$LAN_IP" || return 1
                    break
                fi
                index=$((index + 1))
            done
            mv -f "$RUN/firewall-next.json" "$RUN/firewall-status.json" || return 1
        fi
    fi
}

ts_firewall_remove() {
    local binary item table parent chain
    for binary in iptables ip6tables; do
        which "$binary" >/dev/null 2>&1 || continue
        ts_legacy_cleanup "$binary"
        for item in filter:INPUT:TSKS_INPUT filter:OUTPUT:TSKS_OUTPUT filter:FORWARD:TSKS_FORWARD nat:POSTROUTING:TSKS_POSTROUTING nat:PREROUTING:TSKS_PREROUTING; do
            table=${item%%:*}; item=${item#*:}; parent=${item%%:*}; chain=${item#*:}
            while ts_ipt "$binary" -t "$table" -D "$parent" -j "$chain" 2>/dev/null; do :; done
            ts_ipt "$binary" -t "$table" -F "$chain" 2>/dev/null || :
            ts_ipt "$binary" -t "$table" -X "$chain" 2>/dev/null || :
        done
    done
}

ts_start() {
    ts_config_read || { ts_job_log '设置值无效，请在插件页面重新应用设置'; return 1; }
    ts_cron
    [ "$ENABLE" = 1 ] || { ts_job_log 'Tailscale 未启用'; return 0; }
    [ -x "$DATA/current/tailscaled" ] && [ -x "$DATA/current/tailscale" ] || {
        ts_job_log '未找到已安装的核心，请重新安装插件'; return 1;
    }
    local fresh=0 pid tries state want
    [ -s "$STATE" ] || fresh=1
    rm -f "$RUN/manual-stop"
    # Explicit start also handles WAN restoration with an existing daemon.
    # Give control reconnection the same grace as a newly launched process.
    ts_now >"$RUN/started-at"
    if ! ts_pid_alive; then
        rm -f "$SOCKET"
        # The helper redacts credentials while consuming output and rotates a
        # bounded RAM log. Both children close the inherited lifecycle lock.
        rm -f "$RUN/daemon.pipe"
        "$HELPER" fifo "$RUN/daemon.pipe" || return 1
        "$HELPER" log "$RUN/daemon.log" 65536 9>&- <"$RUN/daemon.pipe" >/dev/null 2>&1 &
        printf '%s\n' "$!" >"$RUN/logger.pid"
        "$DATA/current/tailscaled" --state="$STATE" --socket="$SOCKET" --port=41641 \
            9>&- </dev/null >"$RUN/daemon.pipe" 2>&1 &
        pid=$!
        printf '%s\n' "$pid" >"$RUN/tailscaled.pid"
        ts_job_log '已启动服务，正在检查就绪状态'
    fi
    tries=0
    while [ "$tries" -lt 15 ]; do
        if ts_status_file "$RUN/start-status.json" && [ "$(ts_get "$RUN/start-status.json" ok)" = true ]; then break; fi
        tries=$((tries + 1))
        sleep 1
    done
    if [ "$tries" -ge 15 ]; then ts_job_log '等待本机服务就绪超时，请查看诊断摘要'; return 1; fi
    set -- set --netfilter-mode=on "--accept-routes=$(ts_bool "$ACCEPT")" "--advertise-exit-node=$(ts_bool "$EXIT_NODE")"
    if [ "$ADVERTISE" = 1 ]; then
        ts_lan || { ts_job_log '局域网地址或子网掩码无效，请检查路由器局域网设置'; return 1; }
        set -- "$@" "--advertise-routes=$LAN_CIDR"
    else
        set -- "$@" --advertise-routes=
    fi
    [ "$fresh" = 0 ] || set -- "$@" --accept-dns=false
    ts_cli "$@" >/dev/null 2>&1 || { ts_job_log '无法应用 Tailscale 设置，请检查本机服务状态'; return 1; }
    want=$(ts_get "$RUN/start-status.json" want_running)
    state=$(ts_get "$RUN/start-status.json" backend_state)
    if [ "$want" != true ] || [ "$state" = NeedsLogin ]; then
        # No --reset: existing identity and unexposed preferences are preserved.
        ts_cli up >/dev/null 2>&1 || :
    fi
    ts_firewall_apply || { ts_job_log '防火墙设置失败，请检查防火墙状态和诊断摘要'; return 1; }
    ts_status_file "$RUN/start-status.json" || return 1
    state=$(ts_get "$RUN/start-status.json" backend_state)
    case $state in NeedsLogin|NeedsMachineAuth) ts_job_log '需要登录或设备授权，请前往插件页面完成授权';;
        Running|Starting|Stopped) ts_job_log '服务设置已应用';;
        *) ts_job_log '本机服务状态异常，请查看诊断摘要'; return 1;; esac
}

ts_stop() {
    local pid tries=0
    which cru >/dev/null 2>&1 && cru d tailscale_watchdog >/dev/null 2>&1
    # Lifecycle stops do not call logout/down or edit persisted WantRunning.
    if ts_pid_alive; then
        pid=$(cat "$RUN/tailscaled.pid")
        kill "$pid" 2>/dev/null || :
        while kill -0 "$pid" 2>/dev/null && [ "$tries" -lt 10 ]; do sleep 1; tries=$((tries + 1)); done
        if kill -0 "$pid" 2>/dev/null && ts_pid_alive; then kill -KILL "$pid" 2>/dev/null || :; fi
    fi
    rm -f "$RUN/tailscaled.pid" "$RUN/logger.pid" "$RUN/daemon.pipe" "$SOCKET"
    ts_firewall_remove
    ts_job_log '服务已停止，连接身份已保留'
}

ts_restart() { ts_stop && ts_start; }

ts_watch_read() {
    WD_FAILURES=0; WD_CONTROL=0; WD_LAST=0; WD_HISTORY=
    local key value
    if [ -f "$RUN/watchdog-state" ]; then
        while IFS='=' read -r key value; do
            ts_uint "$value" || continue
            case $key in failures) WD_FAILURES=$value;; control_since) WD_CONTROL=$value;; esac
        done <"$RUN/watchdog-state"
    fi
    if [ -f "${STATE%/*}/watchdog-ledger" ]; then
        while IFS= read -r value; do
            ts_uint "$value" || continue
            if [ "$value" -le "$NOW" ] && [ "$((NOW - value))" -lt 86400 ]; then
                WD_HISTORY="$WD_HISTORY $value"
            fi
            [ "$value" -le "$WD_LAST" ] || WD_LAST=$value
        done <"${STATE%/*}/watchdog-ledger"
    fi
    WD_COUNT=0
    for value in $WD_HISTORY; do WD_COUNT=$((WD_COUNT + 1)); done
}

ts_watch_save() {
    printf 'failures=%s\ncontrol_since=%s\n' "$WD_FAILURES" "$WD_CONTROL" >"$RUN/watchdog-state.new" &&
        mv -f "$RUN/watchdog-state.new" "$RUN/watchdog-state"
}

ts_wan_healthy() {
    local primary connected link code
    primary=$(nvram get wan_primary 2>/dev/null)
    case $primary in 0|1) ;; *) primary=0;; esac
    connected=$(nvram get "wan${primary}_state_t" 2>/dev/null)
    link=$(nvram get "wan${primary}_link" 2>/dev/null)
    [ "$connected" = 2 ] && [ "$link" != 0 ] || return 1
    # Requires a successful validated HTTPS response, not just carrier or ping.
    # /key is the public control-key endpoint (HTTPS 200), without credentials.
    code=$(curl --noproxy '*' -fsS -o /dev/null -w '%{http_code}' --connect-timeout 4 --max-time 8 \
        --proto '=https' --tlsv1.2 https://controlplane.tailscale.com/key 2>/dev/null) || return 1
    [ "$code" = 200 ]
}

ts_watch_confirm() {
    local kind=$1 ok=false state want logged sync monitoring online health
    if ts_status_file "$RUN/watchdog-confirm.json"; then
        ok=$(ts_get "$RUN/watchdog-confirm.json" ok)
        state=$(ts_get "$RUN/watchdog-confirm.json" backend_state)
        want=$(ts_get "$RUN/watchdog-confirm.json" want_running)
        logged=$(ts_get "$RUN/watchdog-confirm.json" logged_out)
        sync=$(ts_get "$RUN/watchdog-confirm.json" sync_enabled)
        case $state in NeedsLogin|NeedsMachineAuth|Stopped) WD_FAILURES=0; WD_CONTROL=0; ts_watch_save; return 1;; esac
        if [ "$want" = false ] || [ "$logged" = true ] || [ "$sync" = false ]; then
            WD_FAILURES=0; WD_CONTROL=0; ts_watch_save; return 1
        fi
    fi
    if [ "$kind" = service ]; then
        if [ "$ok" = true ] && ts_pid_alive; then
            WD_FAILURES=0; WD_CONTROL=0; ts_watch_save; return 1
        fi
        return 0
    fi
    if [ "$ok" = true ] && ts_pid_alive; then
        monitoring=$(ts_get "$RUN/watchdog-confirm.json" monitoring_available)
        online=$(ts_get "$RUN/watchdog-confirm.json" online)
        health=$(ts_get "$RUN/watchdog-confirm.json" health_codes)
        if [ "$monitoring" = true ] && { [ "$online" = false ] || printf '%s' "$health" | grep -q '"mapresponse-timeout"'; }; then
            ts_wan_healthy
            return $?
        fi
    fi
    WD_CONTROL=0; ts_watch_save
    return 1
}

ts_address_maintenance() {
    local sample=$1 ips applied
    [ "$(ts_get "$sample" ok)" = true ] || return 0
    ips=$(ts_get "$sample" ips) || return 0
    case $ips in ''|'[]'|null) return 0;; esac
    applied=$(ts_get "$RUN/firewall-status.json" ips) || applied=
    [ "$ips" != "$applied" ] || [ -f "$RUN/nat-pending" ] || return 0
    # This completes normal address-dependent setup after login or an address
    # change, independently of automatic service recovery. Reuse the sample.
    ts_firewall_apply "$sample" || return 1
    ts_job_log '已根据当前 Tailscale 地址刷新防火墙规则'
}

ts_watchdog_run() {
    # Only explicit lifecycle/core operations recover interrupted transactions.
    # A cron tick must not race or consume recovery budget for a pending update.
    [ ! -e "$DATA/update.txn" ] && [ ! -L "$DATA/update.txn" ] || return 0
    ts_config_read || return 1
    [ "$ENABLE" = 1 ] && [ ! -f "$RUN/manual-stop" ] || return 0
    local sampled=0
    if ts_status_file "$RUN/watchdog-status.json"; then
        sampled=1
        ts_address_maintenance "$RUN/watchdog-status.json" || ts_job_log '根据当前地址刷新防火墙规则失败，将在后续检查中重试'
    fi
    [ "$WATCHDOG" = 1 ] || return 0
    NOW=$(ts_now)
    ts_uint "$NOW" || return 1
    ts_watch_read
    local started state ok want logged sync monitoring health online trigger=0 reason=
    started=$(cat "$RUN/started-at" 2>/dev/null)
    if ! ts_uint "$started"; then
        # A lost RAM directory should grant boot grace once, not on each tick.
        printf '%s\n' "$NOW" >"$RUN/started-at"
        return 0
    fi
    if [ "$NOW" -lt "$started" ] || [ "$((NOW - started))" -lt 180 ]; then return 0; fi
    if [ "$sampled" = 1 ]; then
        ok=$(ts_get "$RUN/watchdog-status.json" ok)
        state=$(ts_get "$RUN/watchdog-status.json" backend_state)
        want=$(ts_get "$RUN/watchdog-status.json" want_running)
        logged=$(ts_get "$RUN/watchdog-status.json" logged_out)
        sync=$(ts_get "$RUN/watchdog-status.json" sync_enabled)
        monitoring=$(ts_get "$RUN/watchdog-status.json" monitoring_available)
        # Login, pending approval and explicit CLI down are deliberate states.
        case $state in NeedsLogin|NeedsMachineAuth|Stopped) WD_FAILURES=0; WD_CONTROL=0; ts_watch_save; return 0;; esac
        if [ "$want" = false ] || [ "$logged" = true ] || [ "$sync" = false ]; then WD_FAILURES=0; WD_CONTROL=0; ts_watch_save; return 0; fi
    else ok=false; fi
    if [ "$ok" != true ] || ! ts_pid_alive; then
        [ "$WD_FAILURES" -ge 3 ] || WD_FAILURES=$((WD_FAILURES + 1))
        WD_CONTROL=0
        [ "$WD_FAILURES" -lt 3 ] || { trigger=service; reason='local service unavailable'; }
    else
        WD_FAILURES=0
        online=$(ts_get "$RUN/watchdog-status.json" online)
        health=$(ts_get "$RUN/watchdog-status.json" health_codes)
        if [ "$monitoring" = true ] && { [ "$online" = false ] || printf '%s' "$health" | grep -q '"mapresponse-timeout"'; }; then
            [ "$WD_CONTROL" -gt 0 ] || WD_CONTROL=$NOW
            if [ "$NOW" -ge "$WD_CONTROL" ] && [ "$((NOW - WD_CONTROL))" -ge 600 ]; then
                trigger=control; reason='control connection unavailable with healthy WAN'
            fi
        else WD_CONTROL=0; fi
    fi
    ts_watch_save
    [ "$trigger" != 0 ] || return 0
    # Clock rollback fails closed. Budget is reserved before restarting so a
    # crash/reboot cannot cause an uncounted recovery loop. Flash writes happen
    # only on a real recovery attempt (maximum two in any rolling 24h).
    [ "$NOW" -ge "$WD_LAST" ] && [ "$((NOW - WD_LAST))" -ge 1800 ] && [ "$WD_COUNT" -lt 2 ] || return 0
    # The service may recover while the probe is running. Require a fresh local
    # sample immediately before spending recovery budget or touching the daemon.
    ts_watch_confirm "$trigger" || return 0
    local ledger="${STATE%/*}/watchdog-ledger" stamp
    : >"$ledger.new"
    for stamp in $WD_HISTORY "$NOW"; do printf '%s\n' "$stamp" >>"$ledger.new"; done
    chmod 600 "$ledger.new" && mv -f "$ledger.new" "$ledger" || return 1
    WD_FAILURES=0; WD_CONTROL=0; ts_watch_save
    ts_job_log "自动恢复尝试，原因：$reason"
    ts_restart
}

ts_status_json() {
    local file="$RUN/status-query.$$" backend=Unavailable online=null codes='[]' messages='[]'
    local auth= monitoring=false version= installed= available= rollback=false error= now statusok=false
    ts_config_read || { ENABLE=0; WATCHDOG=0; error=invalid_configuration; }
    if ts_status_file "$file"; then
        statusok=$(ts_get "$file" ok)
        [ "$statusok" = true ] || error=local_api_unavailable
        backend=$(ts_get "$file" backend_state)
        online=$(ts_get "$file" online)
        codes=$(ts_get "$file" health_codes)
        messages=$(ts_get "$file" health_messages)
        auth=$(ts_get "$file" auth_url)
        monitoring=$(ts_get "$file" monitoring_available)
        version=$(ts_get "$file" version)
    else error=local_api_unavailable; fi
    rm -f "$file"
    case $online in true|false|null) ;; *) online=null;; esac
    case $monitoring in true|false) ;; *) monitoring=false;; esac
    [ -n "$codes" ] || codes='[]'
    [ -n "$messages" ] || messages='[]'
    installed=$(ts_get "$DATA/current/descriptor.json" version) || installed=
    [ -n "$version" ] || version=$installed
    available=$(ts_get "$DATA/available.json" version) || available=
    [ ! -x "$DATA/previous/tailscaled" ] || rollback=true
    NOW=$(ts_now); ts_watch_read
    local last=
    [ "$WD_LAST" = 0 ] || last=$WD_LAST
    printf '{"schema":1,"enabled":%s,"plugin_version":"3.0.0","core_version":%s,"backend_state":%s,"online":%s,"health_codes":%s,"health_messages":%s,"auth_url":%s,"monitoring_available":%s,"watchdog":{"enabled":%s,"last_recovery":%s,"count_24h":%s},"core":{"installed":%s,"available":%s,"can_rollback":%s}' \
        "$(ts_bool "$ENABLE")" "$(ts_quote "$version")" "$(ts_quote "$backend")" "$online" "$codes" "$messages" "$(ts_quote "$auth")" "$monitoring" "$(ts_bool "$WATCHDOG")" "$(ts_quote "$last")" "$WD_COUNT" "$(ts_quote "$installed")" "$(ts_quote "$available")" "$rollback"
    [ -z "$error" ] || printf ',"error":%s' "$(ts_quote "$error")"
    printf '}\n'
}

ts_parse_request() {
    ID=
    if ts_job_id "${1:-}"; then ID=$1; shift; fi
    ACTION=${1:-}
}

ts_mutation() {
    local action=$1 rc=0 bits= has_bits=0 old_running=0 old_manual=0 applied=0
    if [ "$action" = web_submit ] && [ "$#" -gt 1 ]; then
        bits=$2; has_bits=1
        if [ "$#" != 2 ] || ! ts_bits_valid "$bits"; then
            ts_reply '{"accepted":false,"error":"invalid_config_snapshot"}'
            return 1
        fi
    fi
    if ! ts_lock; then
        [ "$action" != start_nat ] || : >"$RUN/nat-pending"
        ts_reply '{"accepted":false,"error":"busy"}'
        return 1
    fi
    trap 'ts_unlock' EXIT
    if [ -n "$ID" ]; then
        ts_job_begin "$ID" || return 1
        ts_reply "{\"accepted\":true,\"job_id\":\"$ID\"}"
    fi
    if [ -f "$KSROOT/scripts/tailscale_core_lib.sh" ]; then
        . "$KSROOT/scripts/tailscale_core_lib.sh" || return 1
        if ! ts_core_recover; then
            ts_job_write failed recovery '上次核心操作尚未恢复，请查看操作日志并生成诊断摘要'
            return 1
        fi
    elif [ -e "$DATA/update.txn" ] || [ -L "$DATA/update.txn" ]; then
        ts_job_write failed recovery '核心恢复组件不可用，请检查插件文件是否完整'
        return 1
    fi
    if [ "$has_bits" = 1 ]; then
        if ! ts_config_snapshot; then
            ts_job_write failed configuration '无法备份当前设置，未应用更改；请检查存储空间后重试'
            return 1
        fi
        ts_pid_alive && old_running=1
        [ ! -f "$RUN/manual-stop" ] || old_manual=1
        if ! ts_config_bits_apply "$bits"; then
            ts_config_restore || ts_job_log '未能完整恢复原设置，请在插件页面检查并重新应用设置'
            ts_job_write failed configuration '设置保存失败，已尝试恢复原设置；请检查操作日志和当前设置'
            return 1
        fi
        applied=1
    fi
    case $action in
        start) ts_start || rc=$?;;
        stop) : >"$RUN/manual-stop"; ts_stop || rc=$?;;
        restart) ts_restart || rc=$?;;
        web_submit)
            if ts_config_read; then
                if [ "$ENABLE" = 1 ]; then ts_restart || rc=$?; else : >"$RUN/manual-stop"; ts_stop || rc=$?; fi
            else ts_job_log '设置值必须为 0 或 1，请在插件页面重新应用设置'; rc=1; fi;;
        start_nat) ts_firewall_apply || rc=$?;;
        *) ts_job_log '无法识别操作请求，请刷新插件页面后重试'; rc=1;;
    esac
    if [ "$rc" != 0 ] && [ "$applied" = 1 ]; then
        if ts_config_restore; then
            ts_job_log '操作失败后已恢复原设置'
            if [ "$old_running" = 1 ]; then
                ts_restart || ts_job_log '原设置已恢复，但服务重启失败；请查看诊断摘要'
            else
                ts_stop || ts_job_log '原设置已恢复，但服务停止失败；请查看诊断摘要'
            fi
            if [ "$old_manual" = 1 ]; then : >"$RUN/manual-stop"; else rm -f "$RUN/manual-stop"; fi
        else
            ts_job_log '原设置恢复失败，请在插件页面检查并重新应用设置'
        fi
    fi
    [ "$has_bits" = 0 ] || rm -f "$RUN/config.previous"
    if [ "$rc" = 0 ]; then ts_job_write success complete '操作已完成'; else ts_job_write failed failed '操作失败，请查看下方日志了解原因'; fi
    ts_unlock
    trap - EXIT
    return "$rc"
}
