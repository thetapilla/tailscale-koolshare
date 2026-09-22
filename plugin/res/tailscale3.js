/* Tailscale Koolshare 3.0.0: one asynchronous request at a time, text-only rendering. */
(function (root, factory) {
    if (typeof module === 'object' && module.exports) { module.exports = factory; }
    else { root.TailscaleUI = { create: factory }; }
}(this, function (options) {
    'use strict';
    var $ = options.$, doc = options.document, clock = options.now || function () { return Date.now(); };
    var later = options.setTimeout || setTimeout, cancel = options.clearTimeout || clearTimeout;
    var storage = options.storage, ask = options.confirm || function () { return true; };
    var keys = ['tailscale_enable', 'tailscale_ipv4_enable', 'tailscale_ipv6_enable', 'tailscale_advertise_routes', 'tailscale_accept_routes', 'tailscale_exit_node', 'tailscale_watchdog_enable'];
    var buttons = ['apply_settings', 'core_check', 'core_update', 'core_rollback', 'run_status', 'run_netcheck', 'run_diagnostics'];
    // Older 32-bit software-center transports may store request IDs as int32.
    var lastId = Math.floor(Math.random() * 1000000000), timer = null, xhr = null;
    var started = false, stopped = false, inFlight = false, epoch = 0;
    var configReady = false, dirty = false, busy = false, pending = null, submitted = null, job = null, logTarget = null;
    var configDue = 0, statusDue = 0, interfacesDue = 0, jobDue = 0, logDue = 0;
    var canUpdate = false, canRollback = false, statusGood = false, lastStatus = null;
    var jobStarted = 0, jobPaused = false, JOB_WINDOW = 15 * 60 * 1000;

    function node(id) { return doc.getElementById(id); }
    function plain(value, limit) {
        return typeof value === 'string' || typeof value === 'number' ? String(value).slice(0, limit || 2048) : '';
    }
    function text(id, value) { var el = node(id); if (el) { el.textContent = plain(value, 8192); } }
    function visible(id, show) { var el = node(id); if (el) { el.style.display = show ? '' : 'none'; } }
    function savePending(value) {
        if (!storage) { return; }
        try {
            if (value) { storage.setItem('tailscale3_pending', JSON.stringify(value)); }
            else { storage.removeItem('tailscale3_pending'); }
        } catch (ignored) { /* Storage may be disabled in the router browser. */ }
    }
    function id() {
        lastId = lastId % 1000000000 + 1;
        return String(lastId);
    }
    function validId(value) { return /^[0-9]{1,15}$/.test(String(value)); }
    function unpack(response) {
        var value = response && response.result;
        if (typeof value === 'string') {
            try { value = JSON.parse(value); } catch (ignored) { return null; }
        }
        return value && typeof value === 'object' ? value : null;
    }
    function safeAuth(value) {
        return typeof value === 'string' && value.length <= 2048 && /^https:\/\/login\.tailscale\.com\/[A-Za-z0-9._~!$&'()*+,;=:@%/?#-]*$/.test(value) ? value : '';
    }
    function controls() {
        var locked = busy || !configReady, i;
        for (i = 0; i < keys.length; i++) { if (node(keys[i])) { node(keys[i]).disabled = locked; } }
        for (i = 0; i < buttons.length; i++) { if (node(buttons[i])) { node(buttons[i]).disabled = locked; } }
        if (node('core_update')) { node('core_update').disabled = locked || !statusGood || !canUpdate; }
        if (node('core_rollback')) { node('core_rollback').disabled = locked || !statusGood || !canRollback; }
        visible('job_resume', jobPaused);
        text('settings_notice', !configReady ? '正在读取设置…' : dirty ? '设置尚未应用' : '');
    }
    function schedule(delay) {
        if (stopped || !started) { return; }
        if (timer !== null) { cancel(timer); }
        timer = later(function () { timer = null; pump(); }, Math.max(0, delay));
    }
    function request(settings, callback) {
        var requestEpoch = epoch, finished = false;
        inFlight = true;
        settings.async = true;
        settings.timeout = 10000;
        settings.cache = false;
        settings.success = function (response) { complete(null, response); };
        settings.error = function (unused, reason) { complete(reason || 'network', null); };
        function complete(error, response) {
            if (finished || stopped || requestEpoch !== epoch) { return; }
            finished = true;
            inFlight = false;
            xhr = null;
            callback(error, response);
            schedule(0);
        }
        try { xhr = $.ajax(settings); }
        catch (error) { complete('network', null); }
    }
    function rpc(method, params, fields, requestId, callback) {
        request({ type: 'POST', url: '/_api/', dataType: 'json', contentType: 'application/json',
            data: JSON.stringify({ id: Number(requestId || id()), method: method, params: params || [], fields: fields || {} }) }, callback);
    }
    function readConfig() {
        request({ type: 'GET', url: '/_api/tailscale_', dataType: 'json' }, function (error, response) {
            var result = unpack(response), data = result && result[0], i;
            if (error || !data || typeof data !== 'object') {
                configDue = clock() + 3000;
                text('connection_notice', '暂时无法读取设置，正在重试…');
                return;
            }
            if (!dirty) {
                for (i = 0; i < keys.length; i++) {
                    if (node(keys[i])) { node(keys[i]).checked = data[keys[i]] === '1' || data[keys[i]] === 1 || data[keys[i]] === true; }
                }
            }
            configReady = true;
            configDue = Infinity;
            text('connection_notice', '');
            controls();
        });
    }
    function renderStatus(data) {
        var core = data.core || {}, watchdog = data.watchdog || {};
        var labels = { Running: '运行中', NeedsLogin: '等待登录', NeedsMachineAuth: '等待设备授权', Stopped: '已停止', Starting: '启动中', NoState: '尚未就绪' };
        var health = Array.isArray(data.health_messages) ? data.health_messages.filter(function (item) { return typeof item === 'string'; }).slice(0, 12).join('\n') : '';
        var auth = safeAuth(data.auth_url), link = node('auth_link');
        lastStatus = data;
        statusGood = true;
        canUpdate = !!plain(core.available);
        canRollback = core.can_rollback === true;
        text('plugin_version', plain(data.plugin_version) || '3.0.0');
        text('core_current', plain(data.core_version) || plain(core.installed) || '未安装');
        text('core_latest', plain(core.available) || '尚未检查');
        text('daemon_state', labels[data.backend_state] || plain(data.backend_state) || '状态未知');
        text('tailnet_state', data.online === true ? '已连接' : data.online === false ? '未连接' : '暂不可用');
        text('monitoring_state', data.monitoring_available === true ? '可用' : '暂不可用');
        text('watchdog_state', (watchdog.enabled === true ? '已启用' : '已关闭') + ' · 24 小时内恢复 ' + (Number(watchdog.count_24h) >= 0 ? Math.floor(Number(watchdog.count_24h)) : 0) + ' 次');
        text('watchdog_recovery', plain(watchdog.last_recovery) || '暂无恢复记录');
        text('health_messages', health);
        visible('health_row', !!health);
        text('connection_notice', data.error ? '状态读取受限：' + plain(data.error) : '');
        if (link) {
            if (auth) { link.setAttribute('href', auth); }
            else { link.removeAttribute('href'); }
        }
        visible('auth_link', !!auth);
        controls();
    }
    function readStatus() {
        rpc('tailscale_fettle', [], '', null, function (error, response) {
            var data = unpack(response);
            if (error || !data || data.schema !== 1 || typeof data.backend_state !== 'string') {
                statusGood = false;
                statusDue = clock() + 5000;
                text('connection_notice', '连接中断或状态暂不可用，正在重试…');
                if (node('auth_link')) { node('auth_link').removeAttribute('href'); }
                visible('auth_link', false);
                controls();
                return;
            }
            renderStatus(data);
            statusDue = clock() + 5000;
        });
    }
    function readInterfaces() {
        rpc('tailscale_tsnets', [1], '', null, function (error, response) {
            var data = unpack(response), body = node('interfaces_body');
            interfacesDue = clock() + (error ? 5000 : 11000);
            if (error || !data || !Array.isArray(data.interfaces)) {
                text('interfaces_notice', '网口状态暂不可用，正在重试…');
                return;
            }
            if (body) {
                while (body.firstChild) { body.removeChild(body.firstChild); }
                data.interfaces.slice(0, 32).forEach(function (item) {
                    var row = doc.createElement('tr');
                    ['if', 'ip', 'rx', 'tx'].forEach(function (key) {
                        var cell = doc.createElement('td');
                        cell.textContent = plain(item && item[key], 256);
                        row.appendChild(cell);
                    });
                    body.appendChild(row);
                });
            }
            text('interfaces_notice', data.interfaces.length ? '' : '暂无 Tailscale 网口；页面会自动刷新。');
        });
    }
    function beginJob(action, uncertain) {
        job = { id: action.id, title: action.title, reloadConfig: action.reloadConfig === true || action.method === 'tailscale_config' };
        jobStarted = clock();
        jobPaused = false;
        jobDue = 0;
        logTarget = null;
        logDue = Infinity;
        savePending(job);
        text('job_id', action.id);
        text('job_title', action.title);
        text('job_state', uncertain ? '正在确认操作是否已接收…' : '已接收，正在处理…');
        text('job_message', uncertain ? '连接暂时中断。正在查询同一任务，请勿重复提交。' : '');
        visible('task_panel', true);
        controls();
    }
    function submit(action) {
        pending = null;
        submitted = action;
        savePending({ id: action.id, title: action.title, reloadConfig: action.method === 'tailscale_config' });
        rpc(action.method, action.params, action.fields, action.id, function (error, response) {
            var result = unpack(response);
            submitted = null;
            if (!error && result && result.accepted === false) {
                savePending(null);
                busy = false;
                text('job_state', result.error === 'busy' ? '有其他操作正在进行，请稍后重试。' : '操作未被接收');
                text('job_message', result.error === 'busy' ? '' : plain(result.error));
                controls();
                return;
            }
            if (error || !result || result.accepted !== true || String(result.job_id) !== action.id) {
                beginJob(action, true);
                return;
            }
            beginJob(action, false);
        });
    }
    function readJob() {
        var expected = job.id;
        rpc('tailscale_job', [expected], '', null, function (error, response) {
            var data = unpack(response), terminal;
            if (!job || job.id !== expected) { return; }
            jobDue = clock() + 1500;
            if (error || !data || data.schema !== 1 || String(data.id) !== expected || !/^(running|success|failed|rolled_back)$/.test(data.state)) {
                jobDue = clock() + 3000;
                text('job_message', '任务状态暂不可用，正在重试。请勿重复提交。');
                return;
            }
            text('job_message', plain(data.message) || plain(data.phase));
            terminal = data.state !== 'running';
            text('job_state', { running: '正在处理…', success: '操作完成', failed: '操作失败', rolled_back: '更新未完成，已恢复上一内核' }[data.state]);
            if (logTarget !== expected) { logDue = 0; }
            logTarget = expected;
            if (terminal) {
                if (job.reloadConfig) { configDue = 0; configReady = false; dirty = false; }
                busy = false;
                job = null;
                jobPaused = false;
                savePending(null);
                logDue = 0;
                statusDue = 0;
                interfacesDue = 0;
                controls();
            }
        });
    }
    function readLog() {
        var expected = logTarget;
        if (!validId(expected)) { logDue = Infinity; return; }
        request({ type: 'GET', url: '/_temp/tailscale3_' + expected + '.log', dataType: 'text' }, function (error, response) {
            if (logTarget !== expected) { return; }
            if (!error && typeof response === 'string') {
                if (node('task_log')) { node('task_log').value = response.slice(-65536); }
                text('log_notice', '');
                logDue = job ? clock() + 2500 : Infinity;
            } else {
                text('log_notice', '日志暂不可用，可稍后重试。');
                logDue = job ? clock() + 5000 : Infinity;
            }
            visible('log_retry', !job && !!error);
        });
    }
    function pump() {
        var now = clock(), due;
        if (stopped || !started || inFlight) { return; }
        if (pending) { submit(pending); return; }
        if (job && !jobPaused && now - jobStarted >= JOB_WINDOW) {
            jobPaused = true;
            text('job_message', '操作耗时较长，暂未确认结果。可继续查询任务进度。');
            controls();
        }
        if (job && !jobPaused && jobDue <= now) { readJob(); return; }
        if (logTarget && logDue <= now) { readLog(); return; }
        if (configDue <= now) { readConfig(); return; }
        if (statusDue <= now) { readStatus(); return; }
        if (interfacesDue <= now) { readInterfaces(); return; }
        due = Math.min(configDue, statusDue, interfacesDue, logTarget ? logDue : Infinity, job && !jobPaused ? jobDue : Infinity);
        schedule(Math.max(100, Math.min(5000, due - now)));
    }
    function action(method, params, title, fields) {
        if (busy || !configReady || stopped) { return false; }
        busy = true;
        pending = { id: id(), method: method, params: params, title: title, fields: fields || {} };
        logTarget = null;
        logDue = Infinity;
        text('task_log', '');
        if (node('task_log')) { node('task_log').value = ''; }
        text('job_id', pending.id);
        text('job_title', title);
        text('job_state', '正在提交…');
        text('job_message', '');
        text('log_notice', '');
        visible('log_retry', false);
        visible('task_panel', true);
        controls();
        schedule(0);
        return true;
    }
    function apply() {
        var bits = '', i;
        if (busy || !configReady) { return false; }
        // httpdb writes fields before dispatch. Keep the snapshot in params so
        // only the locked backend operation can change persisted settings.
        for (i = 0; i < keys.length; i++) { bits += node(keys[i]).checked ? '1' : '0'; }
        return action('tailscale_config', ['web_submit', bits], '应用设置', {});
    }
    function bind(id, event, callback) { if (node(id)) { $(node(id)).on(event, callback); } }
    function init() {
        var saved;
        if (started) { return; }
        started = true;
        stopped = false;
        keys.forEach(function (key) {
            bind(key, 'change', function () {
                if (busy || !configReady) { return; }
                dirty = true;
                controls();
                if (key === 'tailscale_enable') { apply(); }
            });
        });
        bind('apply_settings', 'click', apply);
        bind('core_check', 'click', function () { action('tailscale_core', ['check'], '检查内核更新'); });
        bind('core_update', 'click', function () {
            if (!busy && canUpdate && statusGood && ask('更新内核会短暂中断 Tailscale 连接。是否继续？')) { action('tailscale_core', ['update'], '更新内核'); }
        });
        bind('core_rollback', 'click', function () {
            if (!busy && canRollback && statusGood && ask('回退内核会短暂中断 Tailscale 连接。是否继续？')) { action('tailscale_core', ['rollback'], '回退内核'); }
        });
        bind('run_status', 'click', function () { action('tailscale_status', [], '连接详情'); });
        bind('run_netcheck', 'click', function () { action('tailscale_ncheck', [], '网络检查'); });
        bind('run_diagnostics', 'click', function () { action('tailscale_diagnostics', [], '诊断日志'); });
        bind('job_resume', 'click', function () { if (job) { jobPaused = false; jobStarted = clock(); jobDue = 0; controls(); schedule(0); } });
        bind('log_retry', 'click', function () { if (logTarget) { logDue = 0; visible('log_retry', false); schedule(0); } });
        try { saved = storage && JSON.parse(storage.getItem('tailscale3_pending')); } catch (ignored) { saved = null; }
        if (saved && validId(saved.id)) { lastId = Math.max(lastId, Number(saved.id)); busy = true; beginJob({ id: String(saved.id), title: plain(saved.title) || '上次操作', reloadConfig: saved.reloadConfig === true }, true); }
        controls();
        schedule(0);
    }
    function stop() {
        stopped = true;
        epoch++;
        if (submitted) { beginJob(submitted, true); submitted = null; }
        if (timer !== null) { cancel(timer); timer = null; }
        if (xhr && typeof xhr.abort === 'function') { xhr.abort(); }
        xhr = null;
        inFlight = false;
    }
    function resume() {
        if (!started || !stopped) { return; }
        stopped = false;
        statusGood = false;
        controls();
        statusDue = 0;
        interfacesDue = 0;
        schedule(0);
    }
    return { init: init, stop: stop, resume: resume, apply: apply, safeAuth: safeAuth };
}));
