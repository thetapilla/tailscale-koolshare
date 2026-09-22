<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta http-equiv="X-UA-Compatible" content="IE=Edge" />
<meta http-equiv="Content-Type" content="text/html; charset=utf-8" />
<meta http-equiv="Pragma" content="no-cache" />
<meta http-equiv="Expires" content="-1" />
<link rel="shortcut icon" href="/images/favicon.png" />
<title>软件中心 - Tailscale</title>
<link rel="stylesheet" type="text/css" href="/index_style.css" />
<link rel="stylesheet" type="text/css" href="/form_style.css" />
<link rel="stylesheet" type="text/css" href="/css/element.css" />
<link rel="stylesheet" type="text/css" href="/js/table/table.css" />
<link rel="stylesheet" type="text/css" href="/res/softcenter.css" />
<script type="text/javascript" src="/js/jquery.js"></script>
<script type="text/javascript" src="/state.js"></script>
<script type="text/javascript" src="/general.js"></script>
<script type="text/javascript" src="/popup.js"></script>
<script type="text/javascript" src="/validator.js"></script>
<script type="text/javascript" src="/res/softcenter.js"></script>
<script type="text/javascript" src="/res/tailscale3.js?v=3.0.0"></script>
<style type="text/css">
.FormTitle, .FormTable, .FormTable th, .FormTable td, .FormTable_table, .FormTable_table th, .FormTable_table td {
    font-size:14px; font-family:Roboto-Light,"Microsoft JhengHei",sans-serif;
}
.FormTable th { width:35%; }
.formfonttitle { font-size:18px; margin-left:5px; }
.SimpleNote { padding:5px; line-height:1.7; }
.ts_section { margin-top:12px; }
.ts_help { font-size:12px; color:#d4d8da; line-height:1.6; padding-top:4px; }
.ts_notice { color:#ffdc86; white-space:pre-wrap; overflow-wrap:break-word; line-height:1.6; }
.ts_value { white-space:pre-wrap; overflow-wrap:break-word; }
.ts_actions input { margin:3px 5px 3px 0; }
.ts_actions a { display:inline-block; margin:3px 5px 3px 0; }
.ts_badge { font-size:12px; color:#ddd; margin-left:8px; }
#return_btn { cursor:pointer; float:right; margin:0 10px; }
#task_log { box-sizing:border-box; width:100%; min-height:180px; resize:vertical; background:rgba(0,0,0,.25); color:#fff; border:1px solid #818181; padding:10px; font:12px/1.6 monospace; }
#task_panel { padding:12px; border:1px solid #818181; }
#job_state { font-weight:bold; }
#job_title { font-size:15px; font-weight:bold; }
#interfaces_body td { word-break:break-all; }
#app[skin=ASUSWRT] .ts_box { outline:none; }
#app[skin=ROG] .ts_box { outline:1px solid #91071f; }
#app[skin=TUF] .ts_box { outline:1px solid #ffa523; }
#app[skin=TS] .ts_box { outline:1px solid #2ed9c3; }
input:disabled { opacity:.5; cursor:default; }
</style>
<script type="text/javascript">
var tailscalePage;
function menu_hook(title, tab) {
    tabtitle[tabtitle.length - 1] = new Array('', 'Tailscale');
    tablink[tablink.length - 1] = new Array('', 'Module_tailscale.asp');
}
function init() {
    show_menu(menu_hook);
    var skin = '<% nvram_get("sc_skin"); %>';
    if (/^(ASUSWRT|ROG|TUF|TS)$/.test(skin)) { document.getElementById('app').setAttribute('skin', skin); }
    var storage;
    try { storage = window.sessionStorage; } catch (ignored) { storage = null; }
    tailscalePage = TailscaleUI.create({ $: jQuery, document: document, storage: storage,
        confirm: function (message) { return window.confirm(message); } });
    tailscalePage.init();
    jQuery(window).on('pagehide unload', function () { tailscalePage.stop(); });
    jQuery(window).on('pageshow', function () { tailscalePage.resume(); });
}
</script>
</head>
<body id="app" skin="ASUSWRT" onload="init();">
<div id="TopBanner"></div>
<div id="Loading" class="popup_bg"></div>
<table class="content" align="center" cellpadding="0" cellspacing="0"><tr>
<td width="17">&nbsp;</td>
<td valign="top" width="202"><div id="mainMenu"></div><div id="subMenu"></div></td>
<td valign="top"><div id="tabMenu" class="submenuBlock"></div>
<table width="98%" border="0" align="left" cellpadding="0" cellspacing="0"><tr><td align="left" valign="top">
<table width="760" border="0" cellpadding="5" cellspacing="0" class="FormTitle" id="FormTitle"><tr><td bgcolor="#4D595D" valign="top">
<div>&nbsp;</div>
<img id="return_btn" onclick="reload_Soft_Center();" title="返回软件中心" alt="返回软件中心" src="/images/backprev.png" onmouseover="this.src='/images/backprevclick.png'" onmouseout="this.src='/images/backprev.png'" />
<div class="formfonttitle">Tailscale <span class="ts_badge">插件 <span id="plugin_version">3.0.0</span></span></div>
<div style="margin:10px 5px;" class="splitLine"></div>
<div class="SimpleNote">通过 Tailscale 安全连接您的路由器和设备。</div>
<div id="connection_notice" class="SimpleNote ts_notice" role="status" aria-live="polite"></div>
<div id="tailscale_main" class="ts_box">
<table width="100%" border="1" cellpadding="4" cellspacing="0" class="FormTable">
<thead><tr><td colspan="2">Tailscale - 状态 / 控制</td></tr></thead>
<tr><th><label for="tailscale_enable">启用 Tailscale</label></th><td>
<div class="switch_field" style="display:table-cell;">
<label for="tailscale_enable"><input id="tailscale_enable" class="switch" type="checkbox" style="display:none;" disabled="disabled" />
<div class="switch_container"><div class="switch_bar"></div><div class="switch_circle transition_style"><div></div></div></div></label></div>
</td></tr>
<tr><th>运行状态</th><td><span id="daemon_state">正在读取…</span></td></tr>
<tr><th>Tailnet 连接</th><td><span id="tailnet_state">正在读取…</span>
<a id="auth_link" class="ks_btn" style="display:none;margin-left:8px;" target="_blank" rel="noopener noreferrer">登录并授权</a></td></tr>
<tr id="health_row" style="display:none;"><th>连接提示</th><td><div id="health_messages" class="ts_value"></div></td></tr>
<tr><th>Tailscale 控制台</th><td><a class="ks_btn" href="https://login.tailscale.com/admin" target="_blank" rel="noopener noreferrer">Admin console</a></td></tr>
<tr><th>连接检查</th><td class="ts_actions">
<input id="run_status" class="button_gen" type="button" value="连接详情" disabled="disabled" />
<input id="run_netcheck" class="button_gen" type="button" value="网络检查" disabled="disabled" />
<input id="run_diagnostics" class="button_gen" type="button" value="诊断日志" disabled="disabled" />
</td></tr>
<tr><th>使用网络</th><td>
<label for="tailscale_ipv4_enable">IPv4</label> <input id="tailscale_ipv4_enable" type="checkbox" disabled="disabled" />
<label for="tailscale_ipv6_enable" style="margin-left:12px;">IPv6</label> <input id="tailscale_ipv6_enable" type="checkbox" disabled="disabled" />
<div class="ts_help">允许通过对应的 Tailscale 地址访问路由器及局域网。</div></td></tr>
<tr><th><label for="tailscale_advertise_routes">宣告路由表</label></th><td><input id="tailscale_advertise_routes" type="checkbox" disabled="disabled" />
<div class="ts_help">向 Tailnet 提供本机的局域网路由。启用后需要在控制台批准路由。</div></td></tr>
<tr><th><label for="tailscale_accept_routes">接受路由表</label></th><td><input id="tailscale_accept_routes" type="checkbox" disabled="disabled" />
<div class="ts_help">访问其他设备宣告的网络。各地局域网应使用不同网段；上级路由也宣告本地网段时，请避免重复接受该路由。</div></td></tr>
<tr><th><label for="tailscale_exit_node">互联网出口</label></th><td><input id="tailscale_exit_node" type="checkbox" disabled="disabled" />
<div class="ts_help">将此路由器提供为出口节点。需在控制台批准，并在其他设备上选择此出口。</div></td></tr>
<tr><th><label for="tailscale_watchdog_enable">自动恢复连接</label></th><td><input id="tailscale_watchdog_enable" type="checkbox" disabled="disabled" />
<div class="ts_help">监测服务异常并尝试恢复；关闭 Tailscale 后不会自动启动。</div>
<div id="watchdog_state" class="ts_help"></div>
<div class="ts_help">状态监测：<span id="monitoring_state">正在读取…</span></div>
<div class="ts_help">最近恢复：<span id="watchdog_recovery">正在读取…</span></div></td></tr>
</table></div>
<div class="apply_gen ts_actions"><input id="apply_settings" class="button_gen" type="button" value="应用设置" disabled="disabled" />
<span id="settings_notice" class="ts_notice" aria-live="polite"></span></div>
<div class="SimpleNote ts_help">启用开关立即生效；其余设置点击“应用设置”后生效。应用设置、更新或回退内核会短暂中断 Tailscale 连接。</div>
<div id="tailscale_core" class="ts_box ts_section">
<table width="100%" border="1" cellpadding="4" cellspacing="0" class="FormTable">
<thead><tr><td colspan="2">Tailscale - 内核管理</td></tr></thead>
<tr><th>当前内核</th><td id="core_current" class="ts_value">正在读取…</td></tr>
<tr><th>最新可用内核</th><td id="core_latest" class="ts_value">尚未检查</td></tr>
<tr><th>内核操作</th><td class="ts_actions">
<input id="core_check" class="button_gen" type="button" value="检查更新" disabled="disabled" />
<input id="core_update" class="button_gen" type="button" value="更新内核" disabled="disabled" />
<input id="core_rollback" class="button_gen" type="button" value="回退上一内核" disabled="disabled" />
<div class="ts_help">检查更新后手动安装。更新失败时尝试恢复上一内核。</div>
</td></tr></table></div>
<div id="task_panel" class="ts_box ts_section" style="display:none;" role="region" aria-label="操作进度">
<div><span id="job_title"></span><span class="ts_badge">任务 <span id="job_id"></span></span></div>
<div id="job_state" class="SimpleNote" role="status" aria-live="polite"></div>
<div id="job_message" class="SimpleNote ts_value"></div>
<div class="ts_actions"><input id="job_resume" class="button_gen" type="button" value="继续查询进度" style="display:none;" /></div>
<label for="task_log" class="ts_help">操作日志</label>
<textarea id="task_log" rows="10" readonly="readonly" wrap="off" spellcheck="false"></textarea>
<div id="log_notice" class="ts_help"></div>
<input id="log_retry" class="button_gen" type="button" value="重新读取日志" style="display:none;" />
</div>
<div id="tailscale_tcnets" class="ts_box ts_section">
<table width="100%" border="1" cellpadding="4" cellspacing="0" class="FormTable_table">
<thead><tr><td colspan="4">Tailscale - 网口状态</td></tr>
<tr><th>接口</th><th>IP 地址</th><th>下行</th><th>上行</th></tr></thead>
<tbody id="interfaces_body"></tbody></table>
<div id="interfaces_notice" class="SimpleNote ts_help">正在读取…</div></div>
<div>&nbsp;</div>
</td></tr></table>
</td></tr></table></td><td width="10"></td>
</tr></table>
<div id="footer"></div>
</body></html>
