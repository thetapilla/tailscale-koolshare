#!/bin/sh
# Preserve persistent identity/preferences, and remove only this plugin's files.
set -u
export LC_ALL=C
KSROOT=${TSKS_ROOT:-/koolshare}
[ -f "$KSROOT/scripts/tailscale_lib.sh" ] && [ -x "$KSROOT/bin/tsks-helper" ] || {
    printf '%s\n' '插件文件不完整，保留现状。请先重新安装插件再卸载。' >&2
    exit 1
}
. "$KSROOT/scripts/tailscale_lib.sh"
[ ! -L "$KSROOT/tailscale" ] || { printf '%s\n' '内核目录结构异常，保留现状。' >&2; exit 1; }
ts_init && ts_lock || { printf '%s\n' '另一个操作正在进行，请稍后卸载。' >&2; exit 1; }
trap 'ts_unlock' EXIT
trap 'exit 1' HUP INT TERM
if [ -e "$DATA/update.txn" ] || [ -L "$DATA/update.txn" ]; then
    printf '%s\n' '核心更新恢复尚未完成，请先恢复核心操作再卸载。' >&2
    exit 1
fi
ts_stop || exit 1
# Identity and user settings under configs/tailscale and tailscale_* remain.
for name in tailscale_config tailscale_fettle tailscale_tsnets tailscale_status tailscale_ncheck tailscale_watchdog tailscale_lib.sh tailscale_core tailscale_core_lib.sh tailscale_job tailscale_diagnostics; do
    rm -f "$KSROOT/scripts/$name" || exit 1
done
rm -f "$KSROOT/bin/tailscale" "$KSROOT/bin/tailscaled" "$KSROOT/bin/tailscale.combined" "$KSROOT/bin/tsks-helper" \
    "$KSROOT/init.d/S96tailscale.sh" "$KSROOT/init.d/N96tailscale.sh" \
    "$KSROOT/res/icon-tailscale.png" "$KSROOT/res/tailscale3.js" "$KSROOT/res/LICENSE.plugin-notice.txt" "$KSROOT/res/LICENSE.tailscale.txt" \
    "$KSROOT/webs/Module_tailscale.asp" "$KSROOT/scripts/uninstall_tailscale.sh" || exit 1
for file in "$KSROOT"/res/LICENSE-tailscale*; do
    [ -f "$file" ] || continue
    rm -f "$file" || exit 1
done
# Core cache belongs solely to this plugin. Never recurse through a symlink.
if [ -d "$DATA/cores" ] && [ ! -L "$DATA/cores" ]; then rm -rf "$DATA/cores" || exit 1; fi
rm -f "$DATA/current" "$DATA/previous" "$DATA/release.pub" "$DATA/available.json" "$DATA/available.signed.json" "$DATA/available-release.json" "$DATA/update.state" || exit 1
rmdir "$DATA" 2>/dev/null || :
for key in tailscale_version softcenter_module_tailscale_version softcenter_module_tailscale_install softcenter_module_tailscale_name softcenter_module_tailscale_title softcenter_module_tailscale_description; do
    dbus remove "$key" || exit 1
done
printf '%s\n' "Tailscale 已卸载。连接身份和用户设置保留于 $KSROOT/configs/tailscale。"
