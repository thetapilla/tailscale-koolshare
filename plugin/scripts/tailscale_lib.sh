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
ts_quote() {
    # Versions, build IDs and lifecycle labels need no JSON escapes. Avoid
    # repeatedly starting the compressed helper for these bounded alphabets.
    case $1 in
        *[!A-Za-z0-9._:/-]*) "$HELPER" quote "$1";;
        *) printf '"%s"\n' "$1";;
    esac
}
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
# BusyBox flock cannot bound a blocking wait, so poll the non-blocking lock.
ts_lock_wait() {
    local remaining=$1
    TS_LOCK_WAITED=0
    while ! ts_lock; do
        [ "$remaining" -gt 0 ] || return 1
        sleep 1
        remaining=$((remaining - 1))
        TS_LOCK_WAITED=$((TS_LOCK_WAITED + 1))
    done
}

# RUN is cleared by the reboot whose startup needs explaining. The firmware log
# is the only record of a firmware-triggered operation that survives it.
ts_syslog() {
    which logger >/dev/null 2>&1 || return 0
    logger -t tailscale-plugin "$1" || :
}

ts_reply() {
    if [ -n "${ID:-}" ]; then
        ts_job_id "$ID" || return 1
        # The software-center daemon embeds this body inside a JSON string.
        # Escape with the helper, then remove only its outer quote delimiters.
        local encoded
        encoded=$(ts_quote "$1") || return 1
        encoded=${encoded#\"}; encoded=${encoded%\"}
        curl --noproxy '*' -fsS --connect-timeout 2 --max-time 5 \
                -X POST --data-binary "$encoded" "http://127.0.0.1:3030/_resp/$ID" >/dev/null 2>&1
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
    # RUN/events.log keeps every line as the plugin's event history, which the
    # diagnostic summary shows; TS_EVENTS=0 keeps the summary itself out of it.
    local line="$(date -u '+%Y-%m-%dT%H:%M:%SZ') $1" target
    if [ "${TS_EVENTS:-1}" = 1 ]; then
        target="$RUN/events.log"
        printf '%s\n' "$line" >>"$target"
        if [ "$(wc -c <"$target")" -gt 32768 ]; then
            tail -c 16384 "$target" >"$target.new" && mv -f "$target.new" "$target"
        fi
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

ts_routes_read() {
    # dbus get terminates its output with one newline. A sentinel preserves
    # any whitespace belonging to the value so validation cannot trim it.
    CUSTOM_ROUTES=$(dbus get tailscale_custom_routes 2>/dev/null && printf '.') || return 1
    CUSTOM_ROUTES=${CUSTOM_ROUTES%.}
    CUSTOM_ROUTES=${CUSTOM_ROUTES%"
"}
}

ts_config_read() {
    ENABLE=$(ts_read_bool tailscale_enable 0) &&
    IPV4=$(ts_read_bool tailscale_ipv4_enable 1) &&
    IPV6=$(ts_read_bool tailscale_ipv6_enable 1) &&
    ADVERTISE=$(ts_read_bool tailscale_advertise_routes 1) &&
    ACCEPT=$(ts_read_bool tailscale_accept_routes 1) &&
    EXIT_NODE=$(ts_read_bool tailscale_exit_node 0) &&
    WATCHDOG=$(ts_read_bool tailscale_watchdog_enable 1) &&
    ACCEPT_DNS=$(ts_read_bool tailscale_accept_dns 0) &&
    CUSTOM=$(ts_read_bool tailscale_custom_routes_enable 0) &&
    APP_CONNECTOR=$(ts_read_bool tailscale_app_connector 0) &&
    LAN_ROUTES=$(ts_read_bool tailscale_accept_routes_lan 1) &&
    APPC_PERIODIC=$(ts_read_bool tailscale_app_connector_periodic 0) || return 1
    ts_routes_read || return 1
    # Validate even a disabled list: keep it available for later use, never
    # conceal a corrupt saved value by treating it as an empty list.
    [ -z "$CUSTOM_ROUTES" ] || CUSTOM_ROUTES=$("$HELPER" routes "$CUSTOM_ROUTES" 2>/dev/null) || return 1
}

ts_bits_valid() {
    case ${#1} in 7|9|10|12) ;; *) return 1;; esac
    case $1 in *[!01]*) return 1;; esac
}

# Complete all untrusted request validation before acquiring a task ID or
# writing configuration. The helper owns CIDR parsing and safe error text.
ts_routes_validate() {
    local error_file index entry reason
    ROUTES_VALUE=; ROUTES_DETAIL=
    error_file=$(ts_temp "$RUN/.routes-check.XXXXXX") || return 1
    if ROUTES_VALUE=$("$HELPER" routes-wire "$1" 2>"$error_file"); then
        rm -f "$error_file"
        return 0
    fi
    IFS="$(printf '\t')" read -r index entry reason <"$error_file"
    rm -f "$error_file"
    case $index in ''|*[!0-9]*) ROUTES_DETAIL='无法校验网段，请检查插件文件是否完整';;
        *) ROUTES_DETAIL="$index ${entry}：$reason";; esac
    return 1
}

ts_config_keys() {
    printf '%s\n' tailscale_enable tailscale_ipv4_enable tailscale_ipv6_enable \
        tailscale_advertise_routes tailscale_accept_routes tailscale_exit_node tailscale_watchdog_enable \
        tailscale_accept_dns tailscale_custom_routes_enable tailscale_app_connector \
        tailscale_accept_routes_lan tailscale_app_connector_periodic
}

ts_config_snapshot() {
    local key value count=0 bits=${1:-000000000000}
    [ "$TS_LOCKED" = 1 ] || return 1
    : >"$RUN/config.previous"
    rm -f "$RUN/config.routes.previous" "$RUN/config.routes.invalid"
    for key in $(ts_config_keys); do
        count=$((count + 1))
        [ "$count" -le "${#bits}" ] || break
        value=$(dbus get "$key" 2>/dev/null) || return 1
        case $value in ''|0|1) ;; *) return 1;; esac
        printf '%s=%s\n' "$key" "$value" >>"$RUN/config.previous" || return 1
    done
    [ "${#bits}" != 7 ] || return 0
    ts_routes_read || return 1
    printf '%s' "$CUSTOM_ROUTES" >"$RUN/config.routes.previous" || return 1
    if ! "$HELPER" routes "$CUSTOM_ROUTES" >/dev/null 2>&1; then
        # Permit a valid new submission to repair corrupt saved data. Keep the
        # original bytes separately; no route value is ever shell source.
        : >"$RUN/config.routes.invalid"
    fi
}

ts_config_bits_apply() {
    local remaining=$1 key bit
    [ "$TS_LOCKED" = 1 ] && ts_bits_valid "$remaining" || return 1
    [ "${#remaining}" = 7 ] || [ "$#" = 2 ] || return 1
    for key in $(ts_config_keys); do
        [ -n "$remaining" ] || break
        bit=${remaining%"${remaining#?}"}; remaining=${remaining#?}
        dbus set "$key=$bit" >/dev/null 2>&1 || return 1
    done
    if [ "${#1}" != 7 ]; then
        [ "$#" = 2 ] || return 1
        if [ -n "$2" ]; then dbus set "tailscale_custom_routes=$2" >/dev/null 2>&1
        else dbus remove tailscale_custom_routes >/dev/null 2>&1; fi
    fi
}

ts_config_restore() {
    local key value routes normalized failed=0 invalid_routes=0
    [ "$TS_LOCKED" = 1 ] && [ -f "$RUN/config.previous" ] || return 1
    if [ -f "$RUN/config.routes.previous" ]; then
        routes=$(cat "$RUN/config.routes.previous" && printf '.') || return 1
        routes=${routes%.}
        if normalized=$("$HELPER" routes "$routes" 2>/dev/null); then routes=$normalized
        elif [ -f "$RUN/config.routes.invalid" ]; then
            invalid_routes=1
        else return 1; fi
    fi
    while IFS='=' read -r key value; do
        case $key in tailscale_enable|tailscale_ipv4_enable|tailscale_ipv6_enable|tailscale_advertise_routes|tailscale_accept_routes|tailscale_exit_node|tailscale_watchdog_enable|tailscale_accept_dns|tailscale_custom_routes_enable|tailscale_app_connector|tailscale_accept_routes_lan|tailscale_app_connector_periodic) ;; *) return 1;; esac
        case $value in 0|1) dbus set "$key=$value" >/dev/null 2>&1 || failed=1;;
            '') dbus remove "$key" >/dev/null 2>&1 || failed=1;; *) return 1;; esac
    done <"$RUN/config.previous"
    if [ -f "$RUN/config.routes.previous" ]; then
        if [ -n "$routes" ]; then dbus set "tailscale_custom_routes=$routes" >/dev/null 2>&1 || failed=1
        else dbus remove tailscale_custom_routes >/dev/null 2>&1 || failed=1; fi
    fi
    if [ "$failed" = 0 ] && [ "$invalid_routes" = 1 ]; then
        ts_job_log '已恢复原无效网段设置，请在插件页面修正后重新应用；服务不会宣告无效网段'
    fi
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
    local binary=$1 table=$2 parent=$3 chain=$4 removed=0
    ts_ipt "$binary" -t "$table" -N "$chain" 2>/dev/null || :
    ts_ipt "$binary" -t "$table" -F "$chain" || return 1
    if [ "$table:$parent" = nat:POSTROUTING ]; then
        # Fullcone and other established NAT hooks get first opportunity;
        # plugin MASQUERADE is the final fallback for LAN-to-tailnet traffic.
        # A program restoring the jump meanwhile must not keep this loop, and
        # the lock it holds, going forever.
        while ts_ipt "$binary" -t "$table" -D "$parent" -j "$chain" 2>/dev/null; do
            removed=$((removed + 1))
            [ "$removed" -lt 16 ] || return 1
        done
        ts_ipt "$binary" -t "$table" -A "$parent" -j "$chain"
    elif ! ts_ipt "$binary" -t "$table" -C "$parent" -j "$chain" 2>/dev/null; then
        ts_ipt "$binary" -t "$table" -I "$parent" 1 -j "$chain"
    fi
}

# IPv6 has no LAN subnet setting to read. Use the global addresses the LAN
# interface has now; ip6tables masks each one to its prefix. Call after ts_lan.
# Sets LAN6 to a bounded, newline-separated list of address/length entries.
# Fails, with LAN6 empty, when the addresses cannot be read.
ts_lan6() {
    local listing
    LAN6=
    listing=$(ts_bound 5 ip -6 -o addr show dev "$LAN_IF" scope global 2>/dev/null) || return 1
    LAN6=$(printf '%s\n' "$listing" | awk '
        {
            entry = ""
            for (i = 1; i < NF; i++) if ($i == "inet6") { entry = tolower($(i + 1)); break }
            if (entry !~ /^[0-9a-f:]+\/[0-9]+$/ || entry ~ /^fd7a:115c:a1e0:/) next
            split(entry, part, "/")
            if (part[2] + 0 < 1 || part[2] + 0 > 128 || seen[entry]++) next
            if (++count <= 8) print entry
        }')
}

# A peer accepts only sources inside this device's tailnet addresses and its
# approved routes. Masquerade LAN IPv6 traffic into the tunnel, as the IPv4
# rule does for the LAN subnet. LAN6_NAT=1 when every rule is in place. When
# the addresses cannot be read, LAN6 is "unknown": no cached list matches it,
# so the next minute task applies the rules again. LAN6_RETRY=1 when the
# rules failed for a reason that may pass.
ts_lan6_nat() {
    local prefix
    LAN6_NAT=0; LAN6_RETRY=0
    ts_lan6 || { LAN6=unknown; return 0; }
    which ip6tables >/dev/null 2>&1 || return 0
    if ts_chain ip6tables nat POSTROUTING TSKS_POSTROUTING 2>/dev/null; then
        LAN6_NAT=1
        for prefix in $LAN6; do
            ts_ipt ip6tables -t nat -A TSKS_POSTROUTING -s "$prefix" -o tailscale0 -j MASQUERADE || { LAN6_NAT=0; break; }
        done
        [ "$LAN6_NAT" = 0 ] || return 0
    fi
    # A kernel without IPv6 NAT has no such table, which ip6tables reports
    # with status 3; ts_lan_rules then keeps LAN IPv6 traffic out of the
    # tunnel. Any other failure, such as a busy firewall, is tried again.
    ts_ipt ip6tables -t nat -S POSTROUTING >/dev/null 2>&1
    [ "$?" = 3 ] || LAN6_RETRY=1
}

# tailscaled looks up its table 52 for every packet at priority 5270, so the
# routes it accepts capture forwarded LAN traffic too. It leaves the numbers
# around its rules free for local rules. LAN traffic that must stay off remote
# routes jumps from 5269 to 5271, over that one lookup, so every other rule
# still applies; 5268 first sends tailnet addresses on to 5270. No rule names
# a table, so a name given to table 52 changes nothing. A rule is this
# plugin's only in exactly this form, never by its priority alone.
# Prints FAMILY's wanted rules in priority order.
ts_lan_rules_wanted() {
    local range=100.64.0.0/10
    [ "$ACCEPT" = 1 ] || return 0
    if [ "$1" = -6 ]; then
        range=fd7a:115c:a1e0::/48
        # Without IPv6 masquerading a peer drops LAN IPv6 sources. Keep that
        # traffic on its usual path even when LAN devices may use remote routes.
        [ "$LAN_ROUTES" = 0 ] || [ "$LAN6_NAT" != 1 ] || return 0
    else
        [ "$LAN_ROUTES" = 0 ] || return 0
    fi
    printf '%s\n' "5268 to $range iif $LAN_IF goto 5270" "5269 iif $LAN_IF goto 5271" '5271 nop'
}

# Prints FAMILY's live rules that have one of the forms above, in priority
# order, as "ip rule show" words them (iproute2 prints "to" before "iif")
# without "from all" or kernel flags such as [unresolved]. A delete request
# names only these selectors, and the kernel removes the first rule that has
# them, whatever else it matches on or jumps to. Each line therefore starts
# with "=" when deleting it removes this rule, or "!" when another rule listed
# earlier would be removed instead. Fails when the rules cannot be listed,
# with status 2 when the kernel has no policy routing for FAMILY: then no rule
# can send that family's traffic to table 52.
ts_lan_rules_live() {
    local listing range=100.64.0.0/10
    [ "$1" = -4 ] || range=fd7a:115c:a1e0::/48
    if ! listing=$(ts_bound 5 ip "$1" rule show 2>&1); then
        case $listing in *'not supported'*) return 2;; esac
        return 1
    fi
    printf '%s\n' "$listing" | awk -v range="$range" '
        {
            line = $1; sub(/:$/, "", line)
            for (i = 2; i <= NF; i++) {
                if ($i == "from" && $(i + 1) == "all") { i++; continue }
                if ($i !~ /^\[/) line = line " " $i
            }
            n = split(line, w, " ")
            if (w[1] != "5268" && w[1] != "5269" && w[1] != "5271") next
            iif = ""; to = ""; action = ""
            for (i = 2; i <= n; i++) {
                if (w[i] == "iif") iif = w[i + 1]
                else if (w[i] == "to") to = w[i + 1]
                else if (w[i] == "goto" || w[i] == "nop") action = w[i]
            }
            # What a delete request for a rule of ours here would also match.
            key = ""
            if (w[1] == "5271" && action == "nop") key = "5271"
            else if (w[1] == "5269" && action == "goto" && iif != "") key = "5269 " iif
            else if (w[1] == "5268" && action == "goto" && to == range && iif != "") key = "5268 " iif
            if (line == "5271 nop" ||
                (n == 7 && w[1] == "5268" && w[2] == "to" && w[3] == range && w[4] == "iif" && w[6] == "goto" && w[7] == "5270") ||
                (n == 5 && w[1] == "5269" && w[2] == "iif" && w[4] == "goto" && w[5] == "5271"))
                print (key in other ? "!" : "=") line
            else if (key != "") other[key] = 1
        }'
}

# FAMILY add|del RULE, with RULE in the form above. The kernel refuses to add
# a rule like one it holds, but compares neither the jump target nor the
# conditions a request leaves out. A refusal therefore means another rule is
# in the way, never that this one is in place: the listing showed it was not.
ts_lan_rule_ip() {
    local family=$1 command=$2 pref first second third fourth rest
    read -r pref first second third fourth rest <<EOF
$3
EOF
    case $pref:$first:$third in
        5271:nop:) set -- pref 5271 nop;;
        5268:to:iif) set -- pref 5268 to "$second" iif "$fourth" goto 5270;;
        5269:iif:goto) set -- pref 5269 iif "$second" goto 5271;;
        *) return 1;;
    esac
    ts_bound 5 ip "$family" rule "$command" "$@" >/dev/null 2>&1
}

ts_lan_rule_listed() {
    case "
$2
" in *"
$1
"*) return 0;; esac
    return 1
}

# Brings FAMILY's rules to the wanted set and adds them to LAN_RULES_SET. A
# rule that stays is never removed, so a refresh cannot briefly send LAN
# traffic through table 52. Removal starts with the jump and addition ends
# with it: the lookup is never skipped for tailnet addresses.
ts_lan_rules_sync() {
    local family=$1 wanted live entry line plain= pref kept stuck=0 failed=0
    wanted=$(ts_lan_rules_wanted "$family")
    # Without a listing nothing can be told apart, so nothing is changed.
    live=$(ts_lan_rules_live "$family")
    case $? in 0) ;; 2) return 0;; *) return 1;; esac
    for pref in 5269 5268 5271; do
        kept=0
        while IFS= read -r entry; do
            line=${entry#?}
            case $line in "$pref "*) ;; *) continue;; esac
            plain="$plain$line
"
            # Keep one copy of a wanted rule; remove other forms and copies,
            # but never by a request that would remove another rule first.
            if [ "$kept" = 0 ] && ts_lan_rule_listed "$line" "$wanted"; then kept=1; continue; fi
            # A jump that stays keeps the rules it depends on.
            if [ "$stuck" = 1 ]; then failed=1; continue; fi
            case $entry in '!'*) false;; *) ts_lan_rule_ip "$family" del "$line";; esac || {
                failed=1
                [ "$pref" != 5269 ] || stuck=1
            }
        done <<LIVE
$live
LIVE
    done
    for pref in 5271 5268 5269; do
        while IFS= read -r line; do
            case $line in "$pref "*) ;; *) continue;; esac
            ! ts_lan_rule_listed "$line" "$plain" || continue
            ts_lan_rule_ip "$family" add "$line" && continue
            # The jump must not stay without the rules it depends on. List
            # again: a request for a rule already gone would remove another.
            live=$(ts_lan_rules_live "$family") || return 1
            while IFS= read -r entry; do
                case $entry in '=5269 '*) ts_lan_rule_ip "$family" del "${entry#?}" || :;; esac
            done <<LIVE
$live
LIVE
            return 1
        done <<WANTED
$wanted
WANTED
    done
    [ "$failed" = 0 ] || return 1
    while IFS= read -r line; do
        [ -z "$line" ] || LAN_RULES_SET="$LAN_RULES_SET$family $line
"
    done <<WANTED
$wanted
WANTED
}

# Call after ts_lan and ts_lan6_nat. Fails when the rules may not match.
# LAN_RULES_SET then lists the rules in place, each after its family.
ts_lan_rules() {
    local failed=0
    LAN_RULES_SET=
    ts_lan_rules_sync -4 || failed=1
    ts_lan_rules_sync -6 || failed=1
    return "$failed"
}

# Removes every rule in the forms above.
ts_lan_rules_clear() {
    local ACCEPT=0
    ts_lan_rules
}

# True when a family's rules differ from those the last successful apply put
# in place, as RUN/firewall-rules records them. With none recorded there is
# nothing to check. A family that cannot be listed is unknown, never a change.
ts_lan_rules_changed() {
    local family live recorded
    [ -f "$RUN/firewall-rules" ] || return 1
    for family in -4 -6; do
        live=$(ts_lan_rules_live "$family")
        case $? in 0) ;; 2) live=;; *) continue;; esac
        live=$(printf '%s\n' "$live" | awk -v family="$family" '$0 != "" { print family " " substr($0, 2) }')
        recorded=$(grep -e "^$family " "$RUN/firewall-rules")
        [ "$live" = "$recorded" ] || return 0
    done
    return 1
}

# True when the LAN interface's IPv6 prefixes differ from the applied ones.
# An unreadable list is unknown, never a change.
ts_lan6_changed() {
    ts_lan && ts_lan6 || return 1
    [ "$LAN6" != "$(cat "$RUN/firewall-lan6" 2>/dev/null)" ]
}

ts_firewall_apply() {
    ts_config_read || return 1
    [ "$ENABLE" = 1 ] || { ts_firewall_remove; return; }
    local binary allowed tailip index retry=0 status_source=${1:-}
    # Consume only events that precede this refresh. A concurrent NAT event
    # leaves a new marker for the next minute task while fd9 is held here.
    # Replaying a deferred event renews the rule repairs like handling one.
    [ ! -f "$RUN/nat-pending" ] || rm -f "$RUN/netfilter-repairs"
    rm -f "$RUN/nat-pending"
    # Only a fully successful apply may establish the address cache. A failed
    # refresh must be retried by maintenance even when addresses stay the same.
    rm -f "$RUN/firewall-status.json" "$RUN/firewall-lan6" "$RUN/firewall-nat6" "$RUN/firewall-rules" "$RUN/firewall-retry"
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
        if [ "${TS_RESTORED:-0}" = 1 ]; then
            # install.sh is restoring a release without this library, which
            # neither masquerades LAN IPv6 traffic nor adds these policy rules
            # and could not remove them.
            LAN6=; LAN6_NAT=1
            ts_lan_rules_clear || :
        else
            # A failure that may pass is tried again by the minute task. The
            # first attempt logs it; the retries do not repeat it.
            ts_lan6_nat
            if [ "$LAN6_RETRY" = 1 ]; then
                retry=1
                [ -n "${TS_FIREWALL_RETRIES:-}" ] || ts_job_log '局域网 IPv6 的来源改写设置失败，局域网设备暂时不能经 Tailscale 访问 IPv6 地址；稍后会自动重试，结果可在诊断摘要中查看'
            fi
            [ "$LAN6_NAT" != 1 ] || : >"$RUN/firewall-nat6" || return 1
            if ! ts_lan_rules; then
                retry=1
                [ -n "${TS_FIREWALL_RETRIES:-}" ] || ts_job_log '局域网设备的路由规则设置失败，局域网设备是否使用远程路由可能与设置不符；稍后会自动重试，结果可在诊断摘要中查看'
            elif [ -n "$LAN_RULES_SET" ]; then
                # Maintenance compares the live rules with this record of the
                # rules put in place, so a change made since is found.
                printf '%s' "$LAN_RULES_SET" >"$RUN/firewall-rules" || return 1
            fi
            [ "$retry" = 0 ] || printf '%s\n' "${TS_FIREWALL_RETRIES:-0}" >"$RUN/firewall-retry" || return 1
        fi
        printf '%s\n' "$LAN6" >"$RUN/firewall-lan6" || return 1
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
    else
        # Without the LAN interface no rule can tell LAN traffic apart.
        ts_lan_rules_clear || :
    fi
}

ts_firewall_remove() {
    local binary item table parent chain removed
    rm -f "$RUN/firewall-nat6" "$RUN/firewall-rules" "$RUN/firewall-retry"
    ts_lan_rules_clear || :
    for binary in iptables ip6tables; do
        which "$binary" >/dev/null 2>&1 || continue
        ts_legacy_cleanup "$binary"
        for item in filter:INPUT:TSKS_INPUT filter:OUTPUT:TSKS_OUTPUT filter:FORWARD:TSKS_FORWARD nat:POSTROUTING:TSKS_POSTROUTING nat:PREROUTING:TSKS_PREROUTING; do
            table=${item%%:*}; item=${item#*:}; parent=${item%%:*}; chain=${item#*:}
            removed=0
            while [ "$removed" -lt 16 ] && ts_ipt "$binary" -t "$table" -D "$parent" -j "$chain" 2>/dev/null; do
                removed=$((removed + 1))
            done
            ts_ipt "$binary" -t "$table" -F "$chain" 2>/dev/null || :
            ts_ipt "$binary" -t "$table" -X "$chain" 2>/dev/null || :
        done
    done
}

# The firmware replaces whole tables when it rebuilds its firewall, and its NAT
# event may run before the filter table is replaced. Compare against the live
# IPv4 rules instead of trusting event order; the firmware rebuilds IPv6 filter
# rules in the same pass. A failed listing means unknown, never missing.
ts_hooks_read() {
    # Written to files: a command substitution would discard the cached result
    # of the wait-option probe and repeat it for every listing.
    ts_ipt iptables -t filter -S >"$RUN/rules-filter" 2>/dev/null &&
        ts_ipt iptables -t nat -S >"$RUN/rules-nat" 2>/dev/null
}
ts_hook() { grep -Eq -e "^-A $2 -j $3[[:space:]]*\$" "$RUN/rules-$1"; }

ts_plugin_hooks_present() {
    ts_hook filter INPUT TSKS_INPUT && ts_hook filter OUTPUT TSKS_OUTPUT &&
        ts_hook filter FORWARD TSKS_FORWARD || return 1
    # The NAT chains exist only when the LAN subnet could be determined.
    ts_lan || return 0
    ts_hook nat POSTROUTING TSKS_POSTROUTING && ts_hook nat PREROUTING TSKS_PREROUTING || return 1
    # The firmware rebuilds no IPv6 NAT table, but another program may replace
    # it alone. Check the hook only where masquerading was set up.
    [ -f "$RUN/firewall-nat6" ] || return 0
    ts_ipt ip6tables -t nat -S POSTROUTING >"$RUN/rules-nat6" 2>/dev/null || return 0
    ts_hook nat6 POSTROUTING TSKS_POSTROUTING
}

# tailscaled installs its ts-* chains when it first configures routing and never
# re-adds them. Once the firmware clears them, inbound and forwarded tailnet
# traffic is dropped while the control connection still reports healthy.
# Call after ts_hooks_read. True only when the running daemon lost its rules.
ts_netfilter_lost() {
    [ "$(ts_get "$1" ok)" = true ] && [ "$(ts_get "$1" backend_state)" = Running ] || return 1
    if ts_hook filter INPUT ts-input && ts_hook filter FORWARD ts-forward &&
            ts_hook nat POSTROUTING ts-postrouting; then
        rm -f "$RUN/netfilter-repairs"
        return 1
    fi
    # A restarted daemon has no network map until it reaches the control plane.
    # While the WAN is down, keep the map that still serves traffic inside the LAN.
    ts_wan_connected
}

# Restarting is the supported way to make the daemon install its rules again.
# Only the minute task does it: the firmware may raise several NAT events during
# one rebuild, each before the filter table is replaced, and a restart per event
# would spend every attempt while the rules are still being removed.
# Returns 0 after a successful restart, 1 after a failed one and 2 when it gave
# up: a daemon that never shows its rules must not be restarted every minute.
ts_netfilter_repair() {
    local count
    count=$(cat "$RUN/netfilter-repairs" 2>/dev/null)
    ts_uint "$count" || count=0
    if [ "$count" -ge 3 ]; then
        # Say it once: only a recorded notice is known not to repeat.
        if [ "$count" = 3 ] && { printf '4\n' >"$RUN/netfilter-repairs"; } 2>/dev/null; then
            ts_job_log 'Tailscale 自身的防火墙规则在连续 3 次重启后仍缺失，已停止自动修复；请生成诊断摘要并检查固件防火墙'
        fi
        return 2
    fi
    count=$((count + 1))
    # Fail closed: an attempt that cannot be recorded would never reach the limit.
    { printf '%s\n' "$count" >"$RUN/netfilter-repairs"; } 2>/dev/null &&
        [ "$(cat "$RUN/netfilter-repairs" 2>/dev/null)" = "$count" ] || return 2
    ts_job_log '固件重建防火墙后 Tailscale 自身的规则已丢失，正在重启服务以恢复'
    ts_restart automatic || { ts_job_log '恢复 Tailscale 防火墙规则时服务重启失败，请查看诊断摘要'; return 1; }
}

ts_waiting_for_control() {
    local file=$1 codes
    [ "$(ts_get "$file" ok)" = true ] &&
        [ "$(ts_get "$file" backend_state)" = NoState ] &&
        [ "$(ts_get "$file" have_node_key)" = true ] &&
        [ "$(ts_get "$file" want_running)" = true ] &&
        [ "$(ts_get "$file" logged_out)" = false ] &&
        [ "$(ts_get "$file" health_available)" = true ] || return 1
    codes=$(ts_get "$file" health_codes) || return 1
    case $codes in \[*\]) ;; *) return 1;; esac
    # NoState also represents a failed state store. Only accept a known healthy
    # persisted profile while its control map is still unavailable.
    ! printf '%s\n' "$codes" | grep -q '"state-store-health"'
}

# Start the daemon and wait for its LocalAPI. This primitive
# never edits persisted preferences or requests a connection/login; installer
# rollback uses it to preserve the configuration semantics of the old plugin.
ts_daemon_start() {
    local pid tries
    [ -x "$DATA/current/tailscaled" ] && [ -x "$DATA/current/tailscale" ] || {
        ts_job_log '未找到已安装的核心，请重新安装插件'; return 1;
    }
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
}

# Rollback restores a previous profile, so verify its lifecycle without
# applying this release's managed preferences or initiating authentication.
ts_restore_ready() {
    local tries=0 state codes file="$RUN/restore-status.json"
    while [ "$tries" -lt 5 ]; do
        if ts_status_file "$file" && [ "$(ts_get "$file" ok)" = true ]; then
            state=$(ts_get "$file" backend_state)
            codes=$(ts_get "$file" health_codes) || codes=
            if printf '%s\n' "$codes" | grep -q '"state-store-health"'; then
                ts_job_log '恢复后的状态存储异常，请保留安装备份并查看诊断摘要'
                return 1
            fi
            case $state in
                Running|Starting|Stopped|NeedsLogin|NeedsMachineAuth) return 0;;
                NoState) ts_waiting_for_control "$file" && return 0;;
            esac
        fi
        tries=$((tries + 1))
        [ "$tries" -ge 5 ] || sleep 1
    done
    ts_job_log '恢复后的服务尚未就绪，请保留安装备份并查看诊断摘要'
    return 1
}

ts_start() {
    local startup=${1:-manual}
    case $startup in manual|automatic) ;; *) return 1;; esac
    ts_config_read || { ts_job_log '设置值无效，请在插件页面重新应用设置'; return 1; }
    ts_cron
    # Turning periodic learning off forgets its schedule, so on again it runs
    # at the next minute.
    [ "$APP_CONNECTOR" = 1 ] && [ "$APPC_PERIODIC" = 1 ] || rm -f "$RUN/appc-periodic"
    [ "$ENABLE" = 1 ] || { ts_job_log 'Tailscale 未启用'; return 0; }
    local state want routes= remaining route route_count=0 plan changed=0 current_connector saved_connector
    # Keep LAN devices off remote routes before the daemon can install any.
    # The firewall apply below completes the rules.
    if [ "$LAN_ROUTES" = 0 ] && ts_lan; then
        LAN6_NAT=1
        ts_lan_rules || :
    fi
    ts_daemon_start || return 1
    # Reuse the ready sample's prefs. Do not issue another LocalAPI request;
    # a missing setting is migrated before any managed preference is changed.
    current_connector=$(ts_get "$RUN/start-status.json" app_connector.advertise) || current_connector=
    case $current_connector in true|false) ;;
        *) ts_job_log '无法读取核心当前的应用连接器设置，未应用设置'; return 1;; esac
    if [ "${TS_CONNECTOR_CAPTURE:-0}" = 1 ]; then
        # A first 10-bit submission may create the key before startup. Preserve
        # its original core value if the set succeeds but later setup fails.
        case $current_connector in true) TS_CONNECTOR_BEFORE=1;; false) TS_CONNECTOR_BEFORE=0;; esac
        TS_CONNECTOR_CAPTURE=0
    fi
    saved_connector=$(dbus get tailscale_app_connector 2>/dev/null) || return 1
    if [ -z "$saved_connector" ]; then
        case $current_connector in
            true) APP_CONNECTOR=1; state=开启;;
            false) APP_CONNECTOR=0; state=关闭;;
            *) ts_job_log '无法读取核心当前的应用连接器设置，未应用设置'; return 1;;
        esac
        dbus set "tailscale_app_connector=$APP_CONNECTOR" >/dev/null 2>&1 || {
            ts_job_log '无法保存应用连接器设置，未应用设置'; return 1;
        }
        ts_job_log "已沿用核心当前的应用连接器设置：$state"
    fi
    # Normalize the updater flags in the same edit as other managed settings:
    # clientupdate-free cores reject edits while a persisted Apply=true remains.
    set -- set --netfilter-mode=on "--accept-routes=$(ts_bool "$ACCEPT")" \
        "--accept-dns=$(ts_bool "$ACCEPT_DNS")" "--advertise-connector=$(ts_bool "$APP_CONNECTOR")" --auto-update=false --update-check=false
    if [ "$ADVERTISE" = 1 ]; then
        ts_lan || { ts_job_log '局域网地址或子网掩码无效，请检查路由器局域网设置'; return 1; }
        routes=$LAN_CIDR; route_count=1
    fi
    if [ "$CUSTOM" = 1 ]; then
        remaining=$CUSTOM_ROUTES
        while [ -n "$remaining" ]; do
            route=${remaining%%,*}
            case $remaining in *,*) remaining=${remaining#*,};; *) remaining=;; esac
            case ,$routes, in *,"$route",*) continue;; esac
            routes=${routes:+$routes,}$route
            route_count=$((route_count + 1))
        done
    fi
    if ! plan=$("$HELPER" routes-plan "$SOCKET" "$routes" "$EXIT_NODE" "$APP_CONNECTOR" 2>/dev/null); then
        ts_job_log '无法读取当前宣告列表，未应用设置'
        return 1
    fi
    case $plan in
        keep) ;;
        "set$(printf '\t')"*)
            changed=1
            set -- "$@" "--advertise-exit-node=$(ts_bool "$EXIT_NODE")" "--advertise-routes=${plan#*"$(printf '\t')"}"
            ;;
        *) ts_job_log '宣告列表计算结果无效，未应用设置'; return 1;;
    esac
    ts_cli "$@" >/dev/null 2>&1 || { ts_job_log '无法应用 Tailscale 设置，请检查本机服务状态'; return 1; }
    want=$(ts_get "$RUN/start-status.json" want_running)
    state=$(ts_get "$RUN/start-status.json" backend_state)
    if [ "$startup" = manual ] && { [ "$want" != true ] || [ "$state" = NeedsLogin ]; }; then
        ts_bound 15 "$HELPER" connect "$SOCKET" >/dev/null 2>&1 || {
            ts_job_log '无法发起连接或登录，请查看诊断摘要'; return 1;
        }
    fi
    ts_firewall_apply || { ts_job_log '防火墙设置失败，请检查防火墙状态和诊断摘要'; return 1; }
    ts_status_file "$RUN/start-status.json" || return 1
    state=$(ts_get "$RUN/start-status.json" backend_state)
    case $state in NeedsLogin|NeedsMachineAuth) ts_job_log '需要登录或设备授权，请前往插件页面完成授权';;
        Running|Starting|Stopped) ts_job_log '服务设置已应用';;
        NoState)
            ts_waiting_for_control "$RUN/start-status.json" || {
                ts_job_log '本机服务尚未就绪，无法确认状态存储和连接意图'; return 1;
            }
            ts_job_log '本机服务已就绪，等待控制面同步';;
        *) ts_job_log '本机服务状态异常，请查看诊断摘要'; return 1;; esac
    if [ "$APP_CONNECTOR" = 1 ] && { [ "$changed" = 1 ] || [ "$current_connector" = false ]; }; then
        ts_appc_prime
    fi
    [ "$route_count" = 0 ] || ts_job_log "已宣告 $route_count 条网段：${routes}；请在管理控制台批准"
}

# Self queries are best effort: DNS policy and control-plane propagation may
# delay learning, but neither should roll back an otherwise healthy startup.
ts_appc_prime() {
    local file="$RUN/appc-prime.$$" assigned primed learned wildcards denied
    if ! ts_bound 20 "$HELPER" appc-prime "$SOCKET" >"$file" 2>/dev/null; then
        rm -f "$file"
        ts_job_log '应用连接器：自查暂未完成，将在设备访问所分配域名时学习'
        return 0
    fi
    assigned=$(ts_get "$file" assigned); primed=$(ts_get "$file" primed)
    learned=$(ts_get "$file" learned); wildcards=$(ts_get "$file" wildcards)
    denied=$(ts_get "$file" denied)
    rm -f "$file"
    if ! ts_uint "$assigned" || ! ts_uint "$primed" || ! ts_uint "$learned" || ! ts_uint "$wildcards"; then
        ts_job_log '应用连接器：无法读取自查结果，请查看诊断摘要'
        return 0
    fi
    if [ "$denied" = true ]; then
        ts_job_log '应用连接器：本机查询被访问控制拒绝，将在设备访问这些域名时学习'
    elif [ "$assigned" = 0 ]; then
        ts_job_log '应用连接器：后台未给本机分配应用'
    else
        ts_job_log "应用连接器：已分配 $assigned 个域名，已学习 $learned 个地址"
        if [ "$primed" -lt "$((assigned - wildcards))" ]; then
            ts_job_log '应用连接器：部分域名尚未完成自查，将在设备访问这些域名时继续学习'
        fi
        [ "$wildcards" = 0 ] || ts_job_log '应用连接器：通配域名需要设备查询后才能学习'
    fi
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

ts_restart() { ts_stop && ts_start "$@"; }

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

ts_wan_connected() {
    local primary connected link
    primary=$(nvram get wan_primary 2>/dev/null)
    case $primary in 0|1) ;; *) primary=0;; esac
    connected=$(nvram get "wan${primary}_state_t" 2>/dev/null)
    link=$(nvram get "wan${primary}_link" 2>/dev/null)
    [ "$connected" = 2 ] && [ "$link" != 0 ]
}

ts_wan_healthy() {
    local code
    ts_wan_connected || return 1
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

# Keeps the plugin rules matching the daemon's addresses and the live tables,
# independently of automatic service recovery. LISTED is 1 after ts_hooks_read.
ts_firewall_maintenance() {
    local sample=$1 listed=$2 ips applied reason= retries
    [ "$(ts_get "$sample" ok)" = true ] || return 0
    ips=$(ts_get "$sample" ips) || ips=
    case $ips in ''|'[]'|null) ;; *)
        # This completes address-dependent setup after login or an address change.
        applied=$(ts_get "$RUN/firewall-status.json" ips) || applied=
        [ "$ips" = "$applied" ] || reason='已根据当前 Tailscale 地址刷新防火墙规则';;
    esac
    [ ! -f "$RUN/nat-pending" ] || [ -n "$reason" ] || reason='已在防火墙重建后刷新插件规则'
    if [ -z "$reason" ] && [ "$listed" = 1 ] && ! ts_plugin_hooks_present; then
        reason='插件防火墙规则已被固件重建清除，已重新应用'
    fi
    # A new IPv6 prefix may reach the LAN after the WAN's NAT event.
    if [ -z "$reason" ] && ts_lan6_changed; then
        reason='已根据局域网 IPv6 地址刷新防火墙规则'
    fi
    # No firmware rebuild touches policy rules, but another program may.
    if [ -z "$reason" ] && ts_lan_rules_changed; then
        reason='局域网设备的路由规则被其他程序改动，已重新应用'
    fi
    # A refresh that met a failure which may pass is tried again, three times
    # after each refresh at most; the refresh logged the failure.
    if [ -z "$reason" ] && retries=$(cat "$RUN/firewall-retry" 2>/dev/null) && ts_uint "$retries" && [ "$retries" -lt 3 ]; then
        TS_FIREWALL_RETRIES=$((retries + 1))
        reason=retry
    fi
    [ -n "$reason" ] || return 0
    # Reuse the sample instead of issuing another LocalAPI request.
    ts_firewall_apply "$sample" || return 1
    if [ "$reason" != retry ]; then ts_job_log "$reason"
    elif [ ! -f "$RUN/firewall-retry" ]; then ts_job_log '已补上之前未能设置的防火墙规则'
    fi
}

# Periodic learning repeats the self query every 30 minutes, so learned
# addresses follow DNS changes even when no device asks this connector.
# RUN/appc-periodic keeps the schedule, the last reported count and notice.
ts_appc_periodic() {
    local sample=$1 file="$RUN/appc-periodic" result="$RUN/appc-periodic.$$" now last=0 reported= notice=
    local key value assigned= primed= learned= wildcards= denied=
    if [ "$APP_CONNECTOR" != 1 ] || [ "$APPC_PERIODIC" != 1 ]; then rm -f "$file"; return 0; fi
    [ "$(ts_get "$sample" backend_state)" = Running ] || return 0
    now=$(ts_now)
    ts_uint "$now" || return 0
    if [ -f "$file" ]; then
        while IFS='=' read -r key value; do
            case $key in
                at) ! ts_uint "$value" || last=$value;;
                learned) ! ts_uint "$value" || reported=$value;;
                notice) case $value in failed|denied) notice=$value;; esac;;
            esac
        done <"$file"
    fi
    # A clock set backwards must not postpone learning by the same amount.
    if [ "$last" != 0 ] && [ "$now" -ge "$last" ] && [ "$((now - last))" -lt 1800 ]; then return 0; fi
    if ts_bound 20 "$HELPER" appc-prime "$SOCKET" >"$result" 2>/dev/null; then
        assigned=$(ts_get "$result" assigned); primed=$(ts_get "$result" primed)
        learned=$(ts_get "$result" learned); wildcards=$(ts_get "$result" wildcards)
        denied=$(ts_get "$result" denied)
    fi
    rm -f "$result"
    if ! ts_uint "$assigned" || ! ts_uint "$primed" || ! ts_uint "$learned" || ! ts_uint "$wildcards"; then
        assigned=
    elif [ "$denied" != true ] && [ "$assigned" -gt "$wildcards" ] && [ "$primed" = 0 ]; then
        # Exact domains were queried, but no answer taught the connector.
        assigned=
    fi
    if [ -z "$assigned" ]; then
        [ "$notice" = failed ] || ts_job_log '应用连接器：定期学习暂未完成，30 分钟后重试'
        notice=failed
    elif [ "$denied" = true ]; then
        [ "$notice" = denied ] || ts_job_log '应用连接器：定期学习的查询被访问控制拒绝，30 分钟后重试'
        notice=denied
    else
        notice=
        # Only exact domains are queried; with none, the count was not read.
        if [ "$assigned" -gt "$wildcards" ]; then
            [ "$learned" = "$reported" ] || ts_job_log "应用连接器：定期学习完成，已学习 $learned 个地址"
            reported=$learned
        fi
    fi
    { printf 'at=%s\nlearned=%s\nnotice=%s\n' "$now" "$reported" "$notice" >"$file.new" &&
        mv -f "$file.new" "$file"; } 2>/dev/null || rm -f "$file.new"
}

ts_watchdog_run() {
    # Only explicit lifecycle/core operations recover interrupted transactions.
    # A cron tick must not race or consume recovery budget for a pending update.
    [ ! -e "$DATA/update.txn" ] && [ ! -L "$DATA/update.txn" ] || return 0
    ts_config_read || return 1
    [ "$ENABLE" = 1 ] && [ ! -f "$RUN/manual-stop" ] || return 0
    local sampled=0 listed=0 repair
    if ts_status_file "$RUN/watchdog-status.json"; then
        sampled=1
        ! ts_hooks_read || listed=1
        if [ "$listed" = 1 ] && ts_netfilter_lost "$RUN/watchdog-status.json"; then
            ts_netfilter_repair; repair=$?
            # The restart applied every rule again and made this sample stale.
            [ "$repair" = 2 ] || return "$repair"
        fi
        ts_firewall_maintenance "$RUN/watchdog-status.json" "$listed" || ts_job_log '刷新防火墙规则失败，将在后续检查中重试'
        ts_appc_periodic "$RUN/watchdog-status.json"
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
    # The daemon restores its persisted connection intent. Automatic recovery
    # must not turn an explicit down/logout into a new connection request.
    ts_restart automatic
}

ts_status_json() {
    local file="$RUN/status-query.$$" backend=Unavailable online=null codes='[]' messages='[]'
    local auth= monitoring=false version= version_long= installed= available= rollback=false error= now statusok=false
    local routes='{"advertised":[],"primary":[]}' app_connector='{"advertise":null,"tags":[],"assigned_domains":[],"wildcards":0,"learned":{},"error":"local_api_unavailable"}'
    local installed_build= available_build= update_available=false previous= previous_build= current_target= offered_target= available_arch=
    local cleanup=false keep
    ts_config_read || { ENABLE=0; WATCHDOG=0; error=invalid_configuration; }
    if ts_status_file "$file"; then
        statusok=$(ts_get "$file" ok)
        [ "$statusok" = true ] || [ -n "$error" ] || error=local_api_unavailable
        backend=$(ts_get "$file" backend_state)
        online=$(ts_get "$file" online)
        codes=$(ts_get "$file" health_codes)
        messages=$(ts_get "$file" health_messages)
        auth=$(ts_get "$file" auth_url)
        monitoring=$(ts_get "$file" monitoring_available)
        version=$(ts_get "$file" version)
        version_long=$(ts_get "$file" version_long) || version_long=
        routes=$(ts_get "$file" routes) || routes='{"advertised":[],"primary":[]}'
        app_connector=$(ts_get "$file" app_connector) || app_connector='{"advertise":null,"tags":[],"assigned_domains":[],"wildcards":0,"learned":{},"error":"local_api_unavailable"}'
    else [ -n "$error" ] || error=local_api_unavailable; fi
    rm -f "$file"
    case $online in true|false|null) ;; *) online=null;; esac
    case $monitoring in true|false) ;; *) monitoring=false;; esac
    [ -n "$codes" ] || codes='[]'
    [ -n "$messages" ] || messages='[]'
    installed=$(ts_get "$DATA/current/descriptor.json" version) || installed=
    installed_build=$(ts_get "$DATA/current/descriptor.json" build) || installed_build=
    [ -n "$version" ] || version=$installed
    available=$(ts_get "$DATA/available.json" version) || available=
    available_build=$(ts_get "$DATA/available.json" build) || available_build=
    available_arch=$(ts_get "$DATA/available.json" arch) || available_arch=
    # This library only declares functions. Reuse the updater's pointer
    # validation and compare the exact version/build/architecture target.
    if [ -f "$KSROOT/scripts/tailscale_core_lib.sh" ]; then
        . "$KSROOT/scripts/tailscale_core_lib.sh"
        current_target=$(ts_core_target "$DATA/current") || current_target=
        [ -z "$current_target" ] || ! ts_core_stale "$current_target" || cleanup=true
    fi
    # An unreadable preference keeps the old core: never delete on doubt.
    keep=$(ts_read_bool tailscale_core_keep_previous 1) || keep=1
    case $available:$available_build in *[!0-9.r:]*) current_target=;; esac
    if [ -n "$current_target" ] && printf '%s\n' "$available" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' &&
        printf '%s\n' "$available_build" | grep -Eq '^r[1-9][0-9]*$'; then
        case $available_arch in arm|arm64)
            if [ "$available_arch" = "$(ts_get "$DATA/current/descriptor.json" arch)" ]; then
                offered_target=cores/$available-$available_build-$available_arch
                [ "$current_target" = "$offered_target" ] || update_available=true
            fi
            ;;
        esac
    fi
    # A nonempty offer with update_available=false means the exact current
    # target. Hide an invalid/uncomparable cache rather than label it equal.
    [ -n "$offered_target" ] || { available=; available_build=; }
    [ ! -x "$DATA/previous/tailscaled" ] || rollback=true
    previous=$(ts_get "$DATA/previous/descriptor.json" version) || previous=
    previous_build=$(ts_get "$DATA/previous/descriptor.json" build) || previous_build=
    NOW=$(ts_now); ts_watch_read
    local last=
    [ "$WD_LAST" = 0 ] || last=$WD_LAST
    printf '{"schema":1,"enabled":%s,"plugin_version":"3.5.0","core_version":%s,"backend_state":%s,"online":%s,"health_codes":%s,"health_messages":%s,"auth_url":%s,"monitoring_available":%s,"watchdog":{"enabled":%s,"last_recovery":%s,"count_24h":%s},"core":{"installed":%s,"installed_build":%s,"available":%s,"available_build":%s,"update_available":%s,"previous":%s,"previous_build":%s,"can_rollback":%s,"can_cleanup":%s,"keep_previous":%s}' \
        "$(ts_bool "$ENABLE")" "$(ts_quote "$version")" "$(ts_quote "$backend")" "$online" "$codes" "$messages" "$(ts_quote "$auth")" "$monitoring" "$(ts_bool "$WATCHDOG")" "$(ts_quote "$last")" "$WD_COUNT" "$(ts_quote "$installed")" "$(ts_quote "$installed_build")" "$(ts_quote "$available")" "$(ts_quote "$available_build")" "$update_available" "$(ts_quote "$previous")" "$(ts_quote "$previous_build")" "$rollback" "$cleanup" "$(ts_bool "$keep")"
    printf ',"core_version_long":%s,"routes":%s,"app_connector":%s' "$(ts_quote "$version_long")" "$routes" "$app_connector"
    [ -z "$error" ] || printf ',"error":%s' "$(ts_quote "$error")"
    printf '}\n'
}

ts_parse_request() {
    ID=
    if ts_job_id "${1:-}"; then ID=$1; shift; fi
    ACTION=${1:-}
}

# When the key was absent before a first explicit submission, its actual core
# value is the old setting. Persist that captured value on rollback so a later
# start cannot import the failed edit, including when the old service was off.
ts_connector_restore_migration() {
    case ${TS_CONNECTOR_BEFORE:-} in '') return 0;; 0|1) ;; *) return 1;; esac
    dbus set "tailscale_app_connector=$TS_CONNECTOR_BEFORE" >/dev/null 2>&1 || {
        ts_job_log '原应用连接器设置恢复失败，请在插件页面检查并重新应用设置'
        return 1
    }
}

ts_mutation() {
    local action=$1 rc=0 bits= has_bits=0 routes= old_running=0 old_manual=0 applied=0 patience=0
    local TS_CONNECTOR_CAPTURE=0 TS_CONNECTOR_BEFORE= TS_LOCK_WAITED=0
    if [ "$action" = web_submit ] && [ "$#" -gt 1 ]; then
        bits=$2; has_bits=1
        if ! ts_bits_valid "$bits" || { [ "${#bits}" = 7 ] && [ "$#" != 2 ]; } ||
            { [ "${#bits}" != 7 ] && [ "$#" != 3 ]; }; then
            ts_reply '{"accepted":false,"error":"invalid_config_snapshot"}'
            return 1
        fi
        if [ "${#bits}" != 7 ]; then
            if ! ts_routes_validate "$3"; then
                ts_reply "{\"accepted\":false,\"error\":\"invalid_custom_routes\",\"detail\":$(ts_quote "$ROUTES_DETAIL")}"
                return 1
            fi
            routes=$ROUTES_VALUE
        fi
    fi
    # Nothing retries a firmware boot or WAN event, and the firmware runs its
    # WAN and NAT hooks concurrently. A lifecycle request without a page behind
    # it waits for the lock instead of being dropped; a page gets "busy" at once.
    case $action in start|stop|restart) [ -n "$ID" ] || patience=120;; esac
    if ! ts_lock && ! { [ "$patience" != 0 ] && ts_lock_wait "$patience"; }; then
        [ "$action" != start_nat ] || : >"$RUN/nat-pending"
        [ "$patience" = 0 ] || ts_syslog "$action 未执行：等待其他操作超过 $patience 秒"
        ts_reply '{"accepted":false,"error":"busy"}'
        return 1
    fi
    trap 'ts_unlock' EXIT
    [ "$TS_LOCK_WAITED" = 0 ] || ts_job_log "$action 请求等待其他操作 $TS_LOCK_WAITED 秒后开始执行"
    # An explicit lifecycle request renews the bounded rule-repair attempts.
    # So does a NAT event: a new rebuild explains a new loss of the rules.
    rm -f "$RUN/netfilter-repairs"
    if [ -n "$ID" ]; then
        ts_job_begin "$ID" || return 1
        ts_reply "{\"accepted\":true,\"job_id\":\"$ID\"}"
    fi
    if [ -f "$KSROOT/scripts/tailscale_core_lib.sh" ]; then
        . "$KSROOT/scripts/tailscale_core_lib.sh" || return 1
        if ! ts_core_recover; then
            ts_job_write failed recovery '上次核心操作尚未恢复，请查看操作日志并生成诊断摘要'
            [ -n "$ID" ] || ts_syslog "$action 失败：上次核心操作尚未恢复"
            return 1
        fi
    elif [ -e "$DATA/update.txn" ] || [ -L "$DATA/update.txn" ]; then
        ts_job_write failed recovery '核心恢复组件不可用，请检查插件文件是否完整'
        [ -n "$ID" ] || ts_syslog "$action 失败：核心恢复组件不可用"
        return 1
    fi
    if [ "$has_bits" = 1 ]; then
        if ! ts_config_snapshot "$bits"; then
            ts_job_write failed configuration '无法备份当前设置，未应用更改；请检查存储空间后重试'
            return 1
        fi
        if [ "${#bits}" -ge 10 ] && grep -qx 'tailscale_app_connector=' "$RUN/config.previous"; then
            TS_CONNECTOR_CAPTURE=1
        fi
        ts_pid_alive && old_running=1
        [ ! -f "$RUN/manual-stop" ] || old_manual=1
        if ! ts_config_bits_apply "$bits" "$routes"; then
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
                # Turning periodic learning off forgets its schedule, also when
                # the same submission stops the service.
                [ "$APP_CONNECTOR" = 1 ] && [ "$APPC_PERIODIC" = 1 ] || rm -f "$RUN/appc-periodic"
                if [ "$ENABLE" = 1 ]; then ts_restart || rc=$?; else : >"$RUN/manual-stop"; ts_stop || rc=$?; fi
            else ts_job_log '设置值无效，请在插件页面重新应用设置'; rc=1; fi;;
        start_nat) ts_firewall_apply || rc=$?;;
        *) ts_job_log '无法识别操作请求，请刷新插件页面后重试'; rc=1;;
    esac
    if [ "$rc" != 0 ] && [ "$applied" = 1 ]; then
        if ts_config_restore && ts_connector_restore_migration; then
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
    [ "$has_bits" = 0 ] || rm -f "$RUN/config.previous" "$RUN/config.routes.previous" "$RUN/config.routes.invalid"
    if [ "$rc" = 0 ]; then ts_job_write success complete '操作已完成'; else ts_job_write failed failed '操作失败，请查看下方日志了解原因'; fi
    [ "$rc" = 0 ] || [ -n "$ID" ] || ts_syslog "$action 失败；原因见 $RUN/events.log"
    ts_unlock
    trap - EXIT
    return "$rc"
}
