#!/bin/sh
# Core transactions. Caller sources tailscale_lib.sh, initializes and holds fd9.
ts_core_target() {
    local target name
    target=$(readlink "$1") || return 1
    case $target in cores/*) ;; *) return 1;; esac
    name=${target#cores/}
    case $name in ''|.*|*[!A-Za-z0-9._-]*) return 1;; esac
    [ -d "$DATA/$target" ] || return 1
    printf '%s\n' "$target"
}
ts_core_valid() {
    local dir=$1 expected actual version
    [ -f "$dir/descriptor.json" ] && [ -x "$dir/tailscale.combined" ] || return 1
    expected=$(ts_get "$dir/descriptor.json" binary_sha256) || return 1
    [ "${#expected}" = 64 ] || return 1
    actual=$("$HELPER" sha256 "$dir/tailscale.combined") || {
        ts_job_log '无法读取核心文件的校验值，请检查核心文件和存储状态'; return 1;
    }
    [ "$actual" = "$expected" ] || {
        ts_job_log '核心文件校验失败，已停止切换；请重新下载核心后重试'; return 1;
    }
    version=$("$HELPER" version "$dir/tailscale.combined") || return 1
    [ "$version" = "$(ts_get "$dir/descriptor.json" version)" ]
}
ts_core_journal() {
    printf '{"phase":"%s","old":"%s","new":"%s","had_state":"%s","enabled":"%s"}\n' \
        "$1" "$CORE_OLD" "$CORE_NEW" "$CORE_HAD_STATE" "$CORE_ENABLED" >"$DATA/.update.txn.$$" || return 1
    chmod 600 "$DATA/.update.txn.$$" && mv -f "$DATA/.update.txn.$$" "$DATA/update.txn" && sync
}
ts_core_read_journal() {
    CORE_OLD=$(ts_get "$DATA/update.txn" old) && CORE_NEW=$(ts_get "$DATA/update.txn" new) &&
    CORE_HAD_STATE=$(ts_get "$DATA/update.txn" had_state) && CORE_ENABLED=$(ts_get "$DATA/update.txn" enabled) &&
    CORE_PHASE=$(ts_get "$DATA/update.txn" phase) || return 1
    local item
    for item in "$CORE_OLD" "$CORE_NEW"; do
        case $item in cores/*) ;; *) return 1;; esac
        item=${item#cores/}; case $item in ''|.*|*[!A-Za-z0-9._-]*) return 1;; esac
    done
    case $CORE_HAD_STATE:$CORE_ENABLED in 0:0|0:1|1:0|1:1) ;; *) return 1;; esac
    case $CORE_PHASE in prepared|backed_up|switched|committed) ;; *) return 1;; esac
}
ts_core_recover() {
    [ -f "$DATA/update.txn" ] || return 0
    [ "$TS_LOCKED" = 1 ] || return 1
    ts_core_read_journal || { ts_job_log '核心恢复记录无效，请保留记录并检查诊断摘要'; return 1; }
    if [ "$CORE_PHASE" = committed ]; then
        "$HELPER" atomic-link "$CORE_OLD" "$DATA/previous" || return 1
        rm -f "$DATA/update.txn" "$DATA/update.state"
        sync
        return 0
    fi
    ts_core_valid "$DATA/$CORE_OLD" || return 1
    ts_stop || return 1
    "$HELPER" atomic-link "$CORE_OLD" "$DATA/current" || return 1
    if [ "$CORE_PHASE" = backed_up ] || [ "$CORE_PHASE" = switched ]; then
        if [ "$CORE_HAD_STATE" = 1 ]; then
            [ -s "$DATA/update.state" ] || return 1
            cp -p "$DATA/update.state" "$STATE.recovery" && chmod 600 "$STATE.recovery" && mv -f "$STATE.recovery" "$STATE" || return 1
        else
            # No pre-existing identity: preserve any identity created by a
            # candidate instead of deleting it during a rollback.
            :
        fi
    fi
    sync
    if [ "$CORE_ENABLED" = 1 ]; then ts_start || return 1; fi
    rm -f "$DATA/update.txn" "$DATA/update.state"
    sync
    ts_job_log '已恢复中断操作前的核心'
}
ts_core_check() {
    local arch current version comparison tmp installed_build offered_build
    arch=$(ts_get "$DATA/current/descriptor.json" arch) || return 1
    case $arch in arm|arm64) ;; *) return 1;; esac
    tmp=$RUN/feed-${TS_JOB:-$$}
    ts_job_write running checking '正在检查核心更新'
    "$HELPER" fetch 'https://github.com/thetapilla/tailscale-koolshare/releases/download/core-stable/manifest.json' "$tmp.json" 65536 || return 1
    "$HELPER" verify "$tmp.json" "$DATA/release.pub" "$arch" >"$tmp.descriptor" || { rm -f "$tmp.json" "$tmp.descriptor"; return 1; }
    current=$("$HELPER" version "$DATA/current/tailscale.combined") || return 1
    version=$(ts_get "$tmp.descriptor" version) || return 1
    comparison=$("$HELPER" compare "$version" "$current") || return 1
    [ "$comparison" != -1 ] || { ts_job_log '更新源版本低于当前版本，已停止更新检查；如需回退，请使用“回退上一核心”'; return 1; }
    if [ "$comparison" = 0 ]; then
        installed_build=$(ts_get "$DATA/current/descriptor.json" build) || installed_build=legacy
        offered_build=$(ts_get "$tmp.descriptor" build) || return 1
        case $installed_build in r[1-9]*)
            installed_build=${installed_build#r}; offered_build=${offered_build#r}
            case $installed_build:$offered_build in *[!0-9:]*) return 1;; esac
            [ "$offered_build" -ge "$installed_build" ] || { ts_job_log '更新源构建版本早于当前版本，已停止更新检查'; return 1; }
            ;;
        esac
    fi
    # Verified metadata only; never accept descriptor fields from HTTP params.
    cp "$tmp.descriptor" "$DATA/.available.$$" && chmod 600 "$DATA/.available.$$" && mv -f "$DATA/.available.$$" "$DATA/available.json" || return 1
    mv -f "$tmp.json" "$DATA/available.signed.json"
    rm -f "$tmp.descriptor"
    ts_job_log "核心 $version 的发布信息已通过签名校验"
}
ts_core_space() {
    local required available previous current mem
    required=$1
    case $required in ''|*[!0-9]*) return 1;; esac
    [ "$required" -le 12582912 ] || return 1
    mem=$(awk '/^MemAvailable:/ {print $2; exit}' "$TSKS_PROC/meminfo")
    [ -n "$mem" ] || mem=$(awk '/^MemFree:/ {print $2; exit}' "$TSKS_PROC/meminfo")
    [ "${mem:-0}" -ge 65536 ] || { ts_job_log '可用内存不足：核心更新至少需要 64 MiB，请释放内存后重试'; return 1; }
    available=$(df -Pk "$DATA" | awk 'END {print $4}')
    required=$(( (required + 1023) / 1024 + 8192 ))
    if [ "${available:-0}" -lt "$required" ]; then
        previous=$(ts_core_target "$DATA/previous") || previous=
        current=$(ts_core_target "$DATA/current") || return 1
        if [ -n "$previous" ] && [ "$previous" != "$current" ]; then
            rm -f "$DATA/previous"
            rm -rf "$DATA/$previous"
            available=$(df -Pk "$DATA" | awk 'END {print $4}')
        fi
    fi
    [ "${available:-0}" -ge "$required" ] || { ts_job_log '可用存储不足：核心更新需保留 8 MiB 余量，请释放存储空间后重试'; return 1; }
}
ts_core_health() {
    local old_state=$1 old_id=$2 expected actual actual_long source_commit stamp new_state new_id have_key reason tries=0
    expected=$(ts_get "$DATA/$CORE_NEW/descriptor.json" version) || return 1
    source_commit=$(ts_get "$DATA/$CORE_NEW/descriptor.json" source_commit) || source_commit=
    # Startup already waits for LocalAPI. Allow a few additional samples for a
    # transient socket failure or a node identity still loading from state.
    while [ "$tries" -lt 5 ]; do
        reason=api
        if ts_status_file "$RUN/core-health.json" && [ "$(ts_get "$RUN/core-health.json" ok)" = true ]; then
            actual=$(ts_get "$RUN/core-health.json" version) || actual=
            actual_long=$(ts_get "$RUN/core-health.json" version_long) || actual_long=$actual
            if [ "$actual" != "$expected" ]; then
                ts_job_log "核心自检失败：版本不匹配（预期 ${expected}，实际 ${actual_long}）"
                return 1
            fi
            # The helper normalizes only tagged release stamps. When available,
            # retain their source identity check as well as the release number.
            # Imported legacy cores may have no recorded source commit.
            case $actual_long in "$actual"-t*)
                stamp=${actual_long#"$actual"-t}; stamp=${stamp%%-*}
                if [ "${#stamp}" = 9 ] && [ -n "$source_commit" ] && [ "$source_commit" != null ]; then
                    case $source_commit in "$stamp"*) ;; *)
                        ts_job_log '核心自检失败：运行核心的源码版本与发布记录不符'
                        return 1;;
                    esac
                fi;;
            esac
            new_state=$(ts_get "$RUN/core-health.json" backend_state) || new_state=
            new_id=$(ts_get "$RUN/core-health.json" node_id) || new_id=
            if [ "$new_state" = NoState ] && ! ts_waiting_for_control "$RUN/core-health.json"; then
                ts_job_log '核心自检失败：无法确认状态存储和连接意图'
                return 1
            fi
            if [ "$old_state" = Running ]; then
                case $new_state in NeedsLogin|NeedsMachineAuth)
                    ts_job_log '核心自检失败：已授权设备需要重新登录或批准'
                    return 1;;
                esac
            fi
            if [ -n "$old_id" ] && [ "$old_id" != null ]; then
                have_key=$(ts_get "$RUN/core-health.json" have_node_key) || have_key=
                if [ "$have_key" = false ]; then
                    ts_job_log '核心自检失败：原有设备密钥不可用'
                    return 1
                fi
                if [ -z "$new_id" ] || [ "$new_id" = null ]; then
                    reason=identity
                elif [ "$new_id" != "$old_id" ]; then
                    ts_job_log '核心自检失败：设备身份发生变化'
                    return 1
                else
                    return 0
                fi
            else
                return 0
            fi
        fi
        tries=$((tries + 1))
        [ "$tries" -ge 5 ] || sleep 1
    done
    case $reason in identity)
        # Self.ID comes from the control map, which may not have arrived while
        # offline. A retained node key and a healthy local lifecycle are enough
        # to commit; network reconnection must not turn into a downgrade loop.
        case $new_state:$have_key in Starting:true|Running:true)
            ts_job_log '本机核心检查通过，等待控制面同步设备信息'
            return 0;;
            NoState:true)
                if ts_waiting_for_control "$RUN/core-health.json"; then
                    ts_job_log '本机核心检查通过，等待控制面同步设备信息'
                    return 0
                fi;;
        esac
        ts_job_log '核心自检失败：未能确认原有设备身份';;
        *) ts_job_log '核心自检失败：本机服务接口持续不可用';;
    esac
    return 1
}
ts_core_switch() {
    local old_state old_id identity_ok
    CORE_NEW=$1
    CORE_OLD=$(ts_core_target "$DATA/current") || return 1
    [ "$CORE_NEW" != "$CORE_OLD" ] || return 0
    ts_core_valid "$DATA/$CORE_NEW" && ts_core_valid "$DATA/$CORE_OLD" || return 1
    ts_config_read || return 1
    CORE_ENABLED=$ENABLE; CORE_HAD_STATE=0
    [ ! -s "$STATE" ] || CORE_HAD_STATE=1
    old_state=; old_id=
    if [ "$ENABLE" = 1 ] && ts_status_file "$RUN/pre-update-status.json"; then
        old_state=$(ts_get "$RUN/pre-update-status.json" backend_state) || old_state=
        old_id=$(ts_get "$RUN/pre-update-status.json" node_id) || old_id=
    fi
    ts_core_journal prepared || return 1
    ts_job_write running switching '正在切换核心，连接可能短暂中断'
    if ! ts_stop; then ts_core_recover; return 1; fi
    if [ "$CORE_HAD_STATE" = 1 ]; then
        cp -p "$STATE" "$DATA/update.state" && chmod 600 "$DATA/update.state" || { ts_core_recover; return 1; }
    fi
    ts_core_journal backed_up || { ts_core_recover; return 1; }
    "$HELPER" atomic-link "$CORE_NEW" "$DATA/current" || { ts_core_recover; return 1; }
    ts_core_journal switched || { ts_core_recover; return 1; }
    if [ "$ENABLE" = 1 ]; then
        identity_ok=1
        if ! ts_start; then
            ts_job_log '核心自检失败：新核心启动未完成'
            identity_ok=0
        elif ! ts_core_health "$old_state" "$old_id"; then
            identity_ok=0
        fi
        if [ "$identity_ok" != 1 ]; then
            if ts_core_recover; then
                ts_job_write rolled_back rollback '核心切换后的检查未通过，已恢复上一核心'
                return 2
            fi
            ts_job_log '自动恢复上一核心未完成，已保留恢复记录；请查看诊断摘要'
            return 1
        fi
    fi
    ts_core_journal committed || return 1
    "$HELPER" atomic-link "$CORE_OLD" "$DATA/previous" || return 1
    rm -f "$DATA/update.txn" "$DATA/update.state"
    sync
    ts_job_log '核心切换已完成'
}
ts_core_update() {
    local tmp version build arch target url required current candidate
    ts_core_check || return 1
    tmp=$RUN/core-${TS_JOB:-$$}; mkdir -p "$tmp" || return 1
    cp "$DATA/available.json" "$tmp/descriptor.json" || return 1
    version=$(ts_get "$tmp/descriptor.json" version); build=$(ts_get "$tmp/descriptor.json" build); arch=$(ts_get "$tmp/descriptor.json" arch)
    target=cores/$version-$build-$arch
    current=$(ts_core_target "$DATA/current") || return 1
    if [ "$current" = "$target" ]; then
        [ "$(ts_get "$DATA/current/descriptor.json" binary_sha256)" = "$(ts_get "$tmp/descriptor.json" binary_sha256)" ] && ts_core_valid "$DATA/current" || { rm -rf "$tmp"; return 1; }
        ts_job_log '当前已安装更新源提供的核心，无需更新'; rm -rf "$tmp"; return 0
    fi
    required=$(ts_get "$tmp/descriptor.json" unpacked_size)
    ts_core_space "$required" || { rm -rf "$tmp"; return 1; }
    ts_job_write running downloading '正在下载并验证核心'
    url=$(ts_get "$tmp/descriptor.json" url)
    "$HELPER" fetch "$url" "$tmp/core.tar.gz" 13631488 && "$HELPER" extract "$tmp/core.tar.gz" "$tmp/descriptor.json" "$tmp/core" || { rm -rf "$tmp"; return 1; }
    ts_core_valid "$tmp/core" || { rm -rf "$tmp"; return 1; }
    if [ -e "$DATA/$target" ]; then
        [ "$(ts_get "$DATA/$target/descriptor.json" binary_sha256)" = "$(ts_get "$tmp/descriptor.json" binary_sha256)" ] && ts_core_valid "$DATA/$target" || { rm -rf "$tmp"; return 1; }
    else
        candidate=$DATA/cores/.candidate-${TS_JOB:-$$}
        # The shared lock excludes live writers. A previous interrupted copy
        # with this request id must never be treated as a nested source tree.
        rm -rf "$candidate"
        mkdir -p "$DATA/cores" && cp -Rp "$tmp/core" "$candidate" && \
            mv "$candidate" "$DATA/$target" || { rm -rf "$candidate" "$tmp"; return 1; }
    fi
    rm -rf "$tmp"
    ts_core_switch "$target"
}
ts_core_rollback() {
    local target
    target=$(ts_core_target "$DATA/previous") || { ts_job_log '没有可回退的上一核心'; return 1; }
    # Snapshot the current state in this transaction; never replay old backup
    # state merely because a user requested a later manual rollback.
    ts_core_switch "$target"
}
