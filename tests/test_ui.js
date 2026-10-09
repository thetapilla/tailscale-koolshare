'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const create = require('../plugin/res/tailscale3.js');
const html = fs.readFileSync(path.join(__dirname, '../plugin/webs/Module_tailscale.asp'), 'utf8');
const wire = value => 'b64.' + Buffer.from(value, 'utf8').toString('base64url');
const source = fs.readFileSync(path.join(__dirname, '../plugin/res/tailscale3.js'), 'utf8');

function element(tag = 'div') {
    return { tag, textContent: '', value: '', checked: false, disabled: false, style: {}, attrs: {}, children: [], events: {},
        setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; },
        appendChild(child) { this.children.push(child); }, removeChild(child) { this.children.splice(this.children.indexOf(child), 1); },
        get firstChild() { return this.children[0]; } };
}
function harness(initialStorage = {}) {
    let now = 1790000000000, timerId = 0, simultaneous = 0, maxSimultaneous = 0;
    const timers = new Map(), nodes = {}, requests = [], history = [], storage = { ...initialStorage };
    for (const match of html.matchAll(/\bid="([^"]+)"/g)) { nodes[match[1]] = element(); }
    const $ = el => ({ on(event, handler) { el.events[event] = handler; } });
    $.ajax = settings => {
        simultaneous++; maxSimultaneous = Math.max(maxSimultaneous, simultaneous);
        const record = { settings, body: settings.data && JSON.parse(settings.data), done: false };
        const finish = (error, value) => {
            if (!record.done) { record.done = true; simultaneous--; }
            if (error) { settings.error({}, error); } else { settings.success(value); }
        };
        record.finish = finish;
        requests.push(record); history.push(record);
        return { abort() { finish('abort'); } };
    };
    let confirmations = [];
    const app = create({ $, document: { getElementById: id => nodes[id], createElement: element },
        now: () => now, setTimeout: (fn, delay) => { timers.set(++timerId, { fn, at: now + delay }); return timerId; },
        clearTimeout: id => timers.delete(id), storage: {
            getItem: key => storage[key] || null, setItem: (key, value) => { storage[key] = value; }, removeItem: key => { delete storage[key]; }
        }, confirm: message => { confirmations.push(message); return true; } });
    function advance(ms = 0) {
        const end = now + ms;
        let count = 0;
        while (true) {
            let first;
            for (const [id, timer] of timers) {
                if (timer.at <= end && (!first || timer.at < first[1].at)) { first = [id, timer]; }
            }
            if (!first) { break; }
            if (++count > 10000) { throw new Error('Unbounded timer loop'); }
            timers.delete(first[0]); now = first[1].at; first[1].fn();
        }
        now = end;
    }
    function next(method) {
        advance();
        const request = requests.shift();
        assert.ok(request, 'Expected a pending request');
        if (method) { assert.equal(request.body && request.body.method, method); }
        return request;
    }
    function reply(request, result, string = false) { request.finish(null, { result: string ? JSON.stringify(result) : result }); }
    function status(extra = {}) { return { schema: 1, enabled: true, plugin_version: '3.0.0', core_version: '1.102.4', backend_state: 'Running', online: true,
        health_codes: [], health_messages: [], auth_url: '', monitoring_available: true,
        watchdog: { enabled: true, last_recovery: '', count_24h: 0 }, core: { installed: '1.102.4', available: '1.104.0', update_available: true, can_rollback: true }, ...extra }; }
    function boot() {
        app.init();
        reply(next(), [{ tailscale_enable: '1', tailscale_ipv4_enable: '1', tailscale_ipv6_enable: '1', tailscale_watchdog_enable: '1' }], true);
        reply(next('tailscale_fettle'), status());
        reply(next('tailscale_tsnets'), { interfaces: [] }, true);
        advance();
    }
    function click(id) { assert.equal(nodes[id].disabled, false, id + ' is disabled'); nodes[id].events.click(); advance(); }
    function change(id, value) { nodes[id].checked = value; nodes[id].events.change(); advance(); }
    function rows() { return nodes.custom_routes_rows.children.map(row => ({ input: row.children[0], remove: row.children[1], hint: row.children[2] })); }
    function editRoute(index, value) { const input = rows()[index].input; assert.equal(input.disabled, false); input.value = value; input.events.input(); advance(); }
    function removeRoute(index) { const button = rows()[index].remove; assert.equal(button.disabled, false); button.events.click(); advance(); }
    function accept(request, string = false) { reply(request, { accepted: true, job_id: String(request.body.id) }, string); }
    function finishJob(request, state, extra = {}) { reply(request, { schema: 1, id: request.body.params[0], state, phase: 'done', message: 'Result', ...extra }); }
    return { app, nodes, requests, history, storage, confirmations, advance, next, reply, status, boot, click, change, rows, editRoute, removeRoute, accept, finishJob,
        maxSimultaneous: () => maxSimultaneous, timers };
}

test('native page keeps menu, all four skins, nine options, and unique HTML IDs', () => {
    const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]);
    assert.equal(ids.length, new Set(ids).size);
    for (const key of ['enable', 'ipv4_enable', 'ipv6_enable', 'advertise_routes', 'accept_routes', 'exit_node', 'watchdog_enable', 'accept_dns', 'custom_routes_enable']) { assert.ok(ids.includes('tailscale_' + key)); }
    for (const skin of ['ASUSWRT', 'ROG', 'TUF', 'TS']) { assert.ok(html.includes('[skin=' + skin + ']')); }
    assert.match(html, /show_menu\(menu_hook\)/);
    assert.doesNotMatch(html + source, /vx\.link|innerHTML|\.html\(|async\s*:\s*false|eval\(|document\.write/);
    assert.ok(html.includes('rel="noopener noreferrer"'));
});

test('initial load uses bounded asynchronous same-origin requests and performs no auto update', () => {
    const h = harness(); h.boot();
    assert.equal(h.nodes.core_current.textContent, '1.102.4');
    assert.notEqual(h.nodes.core_current.textContent, h.nodes.plugin_version.textContent);
    assert.equal(h.nodes.core_update.disabled, false);
    assert.deepEqual(h.history.map(r => r.body && r.body.method), [undefined, 'tailscale_fettle', 'tailscale_tsnets']);
    for (const r of h.history) { assert.equal(r.settings.async, true); assert.equal(r.settings.timeout, 10000); }
    assert.equal(h.maxSimultaneous(), 1);
});

test('core identity includes matching builds and the rollback target', () => {
    const h = harness(); h.app.init(); h.reply(h.next(), [{}]);
    h.reply(h.next('tailscale_fettle'), h.status({ core_version: '1.104.1', core: {
        installed: '1.104.1', installed_build: 'r1', available: '1.104.1', available_build: 'r2',
        update_available: true, can_rollback: true, previous: '1.102.4', previous_build: 'legacy'
    } })); h.advance();
    assert.equal(h.nodes.core_current.textContent, '1.104.1 (r1)');
    assert.equal(h.nodes.core_latest.textContent, '1.104.1 (r2)');
    assert.equal(h.nodes.core_update.disabled, false);
    assert.equal(h.nodes.core_rollback_target.textContent, '将回退到 1.102.4（原版核心）');
    assert.equal(h.nodes.core_rollback_target.style.display, '');
});

test('same core after update is labelled and cannot be installed again', () => {
    const h = harness(); h.app.init(); h.reply(h.next(), [{}]);
    h.reply(h.next('tailscale_fettle'), h.status({ core_version: '1.104.1', core: {
        installed: '1.104.1', installed_build: 'r2', available: '1.104.1', available_build: 'r2',
        update_available: false, can_rollback: true, previous: '1.104.1', previous_build: 'r1'
    } })); h.advance();
    assert.equal(h.nodes.core_current.textContent, '1.104.1 (r2)');
    assert.equal(h.nodes.core_latest.textContent, '1.104.1 (r2)（与当前核心相同）');
    assert.equal(h.nodes.core_update.disabled, true);
    assert.equal(h.nodes.core_rollback_target.textContent, '将回退到 1.104.1 (r1)');
});

test('mismatched running versions never inherit a descriptor build and legacy builds are identified', () => {
    for (const [running, installed, build, expected] of [
        ['1.104.1', '1.102.4', 'r2', '1.104.1'],
        ['1.102.4', '1.102.4', 'legacy', '1.102.4（原版核心）'],
        ['1.104.1', '1.104.1', '', '1.104.1'],
        ['1.104.1', '1.104.1', '<img src=x>', '1.104.1']
    ]) {
        const h = harness(); h.app.init(); h.reply(h.next(), [{}]);
        h.reply(h.next('tailscale_fettle'), h.status({ core_version: running,
            core: { installed, installed_build: build, available: '', update_available: false, can_rollback: false } })); h.advance();
        assert.equal(h.nodes.core_current.textContent, expected);
        assert.equal(h.nodes.core_latest.textContent, '暂无检查结果');
        assert.equal(h.nodes.core_rollback_target.style.display, 'none');
    }
});

test('only the strict backend update decision enables installation', () => {
    for (const decision of [undefined, false, 'true', 1, null]) {
        const h = harness(); h.app.init(); h.reply(h.next(), [{}]);
        h.reply(h.next('tailscale_fettle'), h.status({ core: {
            installed: '1.104.1', installed_build: 'r1', available: '1.104.1', available_build: 'r2', update_available: decision
        } })); h.advance();
        assert.equal(h.nodes.core_update.disabled, true);
    }
});

test('an authorization URL arriving after the start task appears on the next status poll', () => {
    const h = harness(); h.app.init(); h.reply(h.next(), [{ tailscale_enable: '1' }]);
    h.reply(h.next('tailscale_fettle'), h.status({ backend_state: 'NeedsLogin', online: false, auth_url: '' }));
    h.reply(h.next('tailscale_tsnets'), { interfaces: [] }); h.advance();
    assert.equal(h.nodes.auth_link.style.display, 'none');
    h.advance(5000);
    h.reply(h.next('tailscale_fettle'), h.status({ backend_state: 'NeedsLogin', online: false,
        auth_url: 'https://login.tailscale.com/a/delayed-test' })); h.advance();
    assert.equal(h.nodes.auth_link.style.display, '');
    assert.equal(h.nodes.auth_link.attrs.href, 'https://login.tailscale.com/a/delayed-test');
    assert.deepEqual(h.history.filter(r => r.body).map(r => r.body.method), ['tailscale_fettle', 'tailscale_tsnets', 'tailscale_fettle']);
});

test('config errors retry; empty interfaces continue polling and stale status disables core changes', () => {
    const h = harness(); h.app.init();
    h.next().finish('timeout'); h.advance(3000);
    // Other read-only polling can proceed while settings retry.
    while (h.requests[0] && h.requests[0].body) {
        const r = h.next(); h.reply(r, r.body.method === 'tailscale_fettle' ? h.status() : { interfaces: [] }); h.advance();
    }
    assert.match(h.nodes.connection_notice.textContent, /设置.*重试/);
    assert.match(h.nodes.settings_notice.textContent, /读取设置失败/);
    h.reply(h.next(), [{ tailscale_enable: '1' }]); h.advance();
    h.advance(5000);
    let r = h.next(); assert.equal(r.body.method, 'tailscale_fettle'); r.finish('timeout'); h.advance();
    assert.equal(h.nodes.core_update.disabled, true);
    assert.match(h.nodes.connection_notice.textContent, /重试/);
    h.advance(5000);
    r = h.next('tailscale_fettle'); h.reply(r, ''); h.advance();
    h.advance(1000);
    r = h.next('tailscale_tsnets'); h.reply(r, { interfaces: [] }); h.advance();
    assert.match(h.nodes.interfaces_notice.textContent, /自动刷新/);
    assert.equal(h.maxSimultaneous(), 1);
});

test('an initial malformed response replaces loading labels and clears after valid status arrives', () => {
    const h = harness(); h.app.init(); h.reply(h.next(), [{ tailscale_enable: '1' }]);
    h.next('tailscale_fettle').finish('parsererror'); h.advance();
    for (const field of ['plugin_version', 'core_current', 'daemon_state', 'tailnet_state', 'monitoring_state', 'watchdog_recovery']) {
        assert.doesNotMatch(h.nodes[field].textContent, /正在读取/);
        assert.match(h.nodes[field].textContent, /无法读取/);
    }
    assert.match(h.nodes.connection_notice.textContent, /返回的状态内容无法读取/);
    h.reply(h.next('tailscale_tsnets'), { interfaces: [] }); h.advance(5000);
    h.reply(h.next('tailscale_fettle'), h.status()); h.advance();
    assert.equal(h.nodes.connection_notice.textContent, '');
    assert.equal(h.nodes.daemon_state.textContent, '运行中');
    assert.equal(h.nodes.core_update.disabled, false);
});

test('slow repeated config and status failures cannot starve interface polling', () => {
    const h = harness(); h.app.init();
    h.advance(10000); h.next().finish('timeout'); h.advance();
    const status = h.next('tailscale_fettle'); h.advance(10000); status.finish('timeout');
    const interfaces = h.next('tailscale_tsnets'); h.reply(interfaces, { interfaces: [] }); h.advance();
    assert.match(h.nodes.settings_notice.textContent, /读取设置失败/);
    assert.match(h.nodes.interfaces_notice.textContent, /自动刷新/);
    assert.equal(h.maxSimultaneous(), 1);
});

test('ACK is not success; one task locks every toggle and action until its matching terminal state', () => {
    const h = harness(); h.boot(); h.click('core_update');
    const write = h.next('tailscale_core');
    assert.deepEqual(write.body.params, ['update']);
    assert.equal(h.confirmations.length, 1);
    for (const id of ['tailscale_enable', 'tailscale_watchdog_enable', 'apply_settings', 'core_check', 'core_update', 'run_netcheck']) { assert.equal(h.nodes[id].disabled, true); }
    h.accept(write, true);
    const poll = h.next('tailscale_job');
    assert.equal(h.nodes.job_state.textContent, '已接收，正在处理…');
    h.finishJob(poll, 'success', { id: String(write.body.id + 1) }); h.advance();
    assert.equal(h.nodes.apply_settings.disabled, true);
    assert.match(h.nodes.job_message.textContent, /重试/);
    h.advance(3000);
    h.finishJob(h.next('tailscale_job'), 'rolled_back');
    h.advance();
    assert.equal(h.nodes.job_state.textContent, '核心切换未完成，已恢复上一核心');
    assert.equal(h.nodes.apply_settings.disabled, false);
    const log = h.next(); assert.match(log.settings.url, /^\/_temp\/tailscale3_[0-9]{1,15}\.log$/);
    log.finish(null, 'restored'); h.advance();
    assert.equal(h.nodes.task_log.value, 'restored');
});

test('lost submission ACK queries same job without repeating a write; unknown jobs never imply success', () => {
    const h = harness(); h.boot(); h.click('run_diagnostics');
    const write = h.next('tailscale_diagnostics'); write.finish('timeout');
    const poll = h.next('tailscale_job');
    assert.equal(poll.body.params[0], String(write.body.id));
    h.finishJob(poll, 'unknown'); h.advance();
    assert.equal(h.nodes.apply_settings.disabled, true);
    h.advance(3000); h.finishJob(h.next('tailscale_job'), 'failed'); h.advance();
    assert.equal(h.nodes.job_state.textContent, '操作失败');
    assert.equal(h.history.filter(r => r.body && r.body.method === 'tailscale_diagnostics').length, 1);
});

test('busy rejection releases controls and preserves draft settings', () => {
    const h = harness(); h.boot(); h.change('tailscale_accept_routes', true); h.click('apply_settings');
    const write = h.next('tailscale_config');
    assert.deepEqual(write.body.fields, {});
    assert.deepEqual(write.body.params, ['web_submit', '111010100', 'b64.']);
    h.reply(write, { accepted: false, error: 'busy' }); h.advance();
    assert.equal(h.nodes.apply_settings.disabled, false);
    assert.equal(h.nodes.tailscale_accept_routes.checked, true);
    assert.match(h.nodes.settings_notice.textContent, /尚未应用/);
    assert.equal(h.storage.tailscale3_pending, undefined);
});

test('switch only edits the draft; Apply submits once and locks concurrent changes', () => {
    const h = harness(); h.boot(); h.change('tailscale_enable', false);
    assert.equal(h.requests.length, 0);
    assert.match(h.nodes.settings_notice.textContent, /尚未应用/);
    h.click('apply_settings');
    assert.equal(h.nodes.settings_notice.textContent, '正在提交设置…');
    const write = h.next('tailscale_config');
    assert.deepEqual(write.body.fields, {});
    assert.deepEqual(write.body.params, ['web_submit', '011000100', 'b64.']);
    assert.equal(h.app.apply(), false);
    h.accept(write); h.finishJob(h.next('tailscale_job'), 'success'); h.advance();
    const log = h.next(); log.finish(null, 'stopped');
    const cfg = h.next(); assert.equal(cfg.settings.url, '/_api/tailscale_');
    h.reply(cfg, [{ tailscale_enable: '0' }]); h.advance();
    assert.equal(h.nodes.tailscale_enable.checked, false);
    assert.equal(h.nodes.apply_settings.disabled, false);
    assert.equal(h.history.filter(r => r.body && r.body.method === 'tailscale_config').length, 1);
});

test('status, interfaces, log and messages are plain text; auth URLs are tightly constrained', () => {
    const h = harness(); h.app.init(); h.reply(h.next(), [{}]);
    const xss = '<img src=x onerror=alert(1)>';
    h.reply(h.next('tailscale_fettle'), h.status({ core_version: xss, health_messages: [xss], auth_url: 'javascript:alert(1)' }));
    h.reply(h.next('tailscale_tsnets'), { interfaces: [{ if: xss, ip: xss, rx: xss, tx: xss }] }); h.advance();
    assert.equal(h.nodes.core_current.textContent, xss);
    assert.equal(h.nodes.health_messages.textContent, xss);
    assert.equal(h.nodes.interfaces_body.children[0].children[0].textContent, xss);
    assert.equal(h.nodes.auth_link.attrs.href, undefined);
    const good = 'https://login.tailscale.com/a/123abc';
    assert.equal(h.app.safeAuth(good), good);
    for (const unsafe of ['//login.tailscale.com/a/a', 'http://login.tailscale.com/a/a', 'https://login.tailscale.com.evil/a', 'https://login.tailscale.com@evil/a', 'https://login.tailscale.com:443/a', 'https://login.tailscale.com\\@evil/a', 'https://login.tailscale.com/a/\n', 'https://evil.com']) { assert.equal(h.app.safeAuth(unsafe), ''); }
    h.click('run_status'); const write = h.next('tailscale_status'); h.accept(write);
    h.finishJob(h.next('tailscale_job'), 'success', { message: xss, log_url: 'https://evil.com/payload' });
    const log = h.next(); log.finish(null, xss); h.advance();
    assert.equal(h.nodes.task_log.value, xss);
    assert.equal(h.nodes.job_message.textContent, xss);
    assert.ok(h.history.every(r => /^\/_api\/(?:tailscale_)?$/.test(r.settings.url) || /^\/_temp\/tailscale3_[0-9]{1,15}\.log$/.test(r.settings.url)));
});

test('invalid or wrong ACK ID cannot switch the UI to another job', () => {
    const h = harness(); h.boot(); h.click('run_netcheck');
    const write = h.next('tailscale_ncheck'); h.reply(write, { accepted: true, job_id: '../../private' });
    const poll = h.next('tailscale_job'); assert.equal(poll.body.params[0], String(write.body.id));
    assert.match(h.nodes.job_state.textContent, /确认/);
});

test('poll request IDs remain unique, an in-flight poll queues a user action, and stale callbacks are ignored', () => {
    const h = harness(); h.boot(); h.advance(5000);
    const old = h.next('tailscale_fettle');
    h.click('core_check'); assert.equal(h.requests.length, 0);
    h.reply(old, h.status()); const check = h.next('tailscale_core');
    h.accept(check); const poll = h.next('tailscale_job');
    h.app.stop(); h.reply(poll, { schema: 1, id: poll.body.params[0], state: 'success', message: 'stale' });
    assert.notEqual(h.nodes.job_message.textContent, 'stale');
    assert.equal(h.timers.size, 0);
    h.app.resume(); h.advance();
    assert.equal(h.maxSimultaneous(), 1);
    const ids = h.history.filter(r => r.body).map(r => r.body.id);
    assert.equal(ids.length, new Set(ids).size);
    assert.ok(ids.every(id => /^[0-9]{1,15}$/.test(String(id))));
    assert.ok(ids.every(id => Number.isInteger(id) && id > 0 && id <= 99999999));
});

test('request IDs wrap within the eight-digit httpdb limit', () => {
    const random = Math.random;
    let h;
    try {
        Math.random = () => 99999998 / 99999999;
        h = harness();
    } finally { Math.random = random; }
    h.boot();
    assert.deepEqual(h.history.filter(r => r.body).map(r => r.body.id), [99999999, 1]);
    h.click('run_status'); const request = h.next('tailscale_status');
    assert.equal(request.body.id, 2);
    h.accept(request); h.finishJob(h.next('tailscale_job'), 'success'); h.advance();
    assert.equal(h.nodes.apply_settings.disabled, false);
});

test('navigation during submission preserves the known job and resumes polling without resubmitting', () => {
    const h = harness(); h.boot(); h.click('core_check'); const write = h.next('tailscale_core');
    h.app.stop(); h.accept(write); h.app.resume();
    const resumed = h.next('tailscale_job');
    assert.equal(resumed.body.params[0], String(write.body.id));
    assert.equal(h.history.filter(r => r.body && r.body.method === 'tailscale_core').length, 1);
    const saved = h.storage.tailscale3_pending;
    const reload = harness({ tailscale3_pending: saved }); reload.app.init();
    assert.equal(reload.next('tailscale_job').body.params[0], String(write.body.id));
});

test('diagnostics completion preserves unsaved option changes and retries missing log on demand', () => {
    const h = harness(); h.boot(); h.change('tailscale_accept_routes', true); h.click('run_diagnostics');
    h.accept(h.next('tailscale_diagnostics')); h.finishJob(h.next('tailscale_job'), 'success');
    h.next().finish('error'); h.advance();
    assert.equal(h.nodes.tailscale_accept_routes.checked, true);
    assert.match(h.nodes.settings_notice.textContent, /尚未应用/);
    assert.equal(h.nodes.log_retry.style.display, '');
});

test('a resumed diagnostic still loads settings correctly before it finishes', () => {
    const h = harness({ tailscale3_pending: JSON.stringify({ id: '179000000000001', title: '生成诊断摘要' }) });
    h.app.init();
    const resumed = h.next('tailscale_job');
    assert.ok(resumed.body.id > 0 && resumed.body.id <= 99999999);
    assert.deepEqual(resumed.body.params, ['179000000000001']);
    h.finishJob(resumed, 'running');
    h.next().finish(null, 'working');
    h.reply(h.next(), [{ tailscale_enable: '1', tailscale_accept_routes: '1', tailscale_watchdog_enable: '1' }]);
    h.reply(h.next('tailscale_fettle'), h.status());
    h.reply(h.next('tailscale_tsnets'), { interfaces: [] }); h.advance(1500);
    h.finishJob(h.next('tailscale_job'), 'success'); h.advance();
    assert.equal(h.nodes.tailscale_enable.checked, true);
    assert.equal(h.nodes.tailscale_accept_routes.checked, true);
    assert.equal(h.nodes.tailscale_watchdog_enable.checked, true);
    assert.equal(h.nodes.apply_settings.disabled, false);
});

test('a missing resumed task releases tracking only after the backend proves idle and settings reload', () => {
    const h = harness({ tailscale3_pending: JSON.stringify({ id: '34567', title: '应用设置', reloadConfig: true }) });
    h.app.init();
    h.finishJob(h.next('tailscale_job'), 'unknown', { id: '34568', operation_busy: false });
    assert.equal(h.nodes.apply_settings.disabled, true);
    h.reply(h.next(), [{ tailscale_enable: '1' }]); h.reply(h.next('tailscale_fettle'), h.status());
    h.reply(h.next('tailscale_tsnets'), { interfaces: [] }); h.advance(3000);
    h.finishJob(h.next('tailscale_job'), 'unknown', { operation_busy: true });
    assert.equal(h.nodes.apply_settings.disabled, true);
    h.advance(3000);
    // A due status read may run before the retrying job.
    if (h.requests[0].body.method === 'tailscale_fettle') { h.reply(h.next(), h.status()); }
    h.finishJob(h.next('tailscale_job'), 'unknown', { operation_busy: false });
    assert.equal(h.nodes.apply_settings.disabled, true);
    assert.equal(h.storage.tailscale3_pending, undefined);
    assert.equal(h.nodes.job_state.textContent, '任务结果无法确认');
    h.reply(h.next(), [{ tailscale_enable: '0' }]); h.reply(h.next('tailscale_fettle'), h.status({ enabled: false }));
    h.reply(h.next('tailscale_tsnets'), { interfaces: [] }); h.advance();
    assert.equal(h.nodes.apply_settings.disabled, false);
    assert.equal(h.nodes.tailscale_enable.checked, false);
    assert.ok(h.history.filter(r => r.body).every(r => ['tailscale_job', 'tailscale_fettle', 'tailscale_tsnets'].includes(r.body.method)));
});

test('long unknown task pauses with explicit resume and never unlocks writes or guesses success', () => {
    const h = harness(); h.boot(); h.click('core_check'); h.accept(h.next('tailscale_core'));
    const poll = h.next('tailscale_job'); h.advance(15 * 60 * 1000);
    poll.finish('timeout'); h.advance();
    assert.equal(h.nodes.job_resume.style.display, '');
    assert.equal(h.nodes.apply_settings.disabled, true);
    assert.match(h.nodes.job_message.textContent, /暂未确认结果/);
    h.click('job_resume');
    // A periodic status read may already be pending when the user resumes.
    if (h.requests[0] && h.requests[0].body.method === 'tailscale_fettle') { h.reply(h.next(), h.status()); }
    const resumed = h.next('tailscale_job');
    assert.equal(resumed.body.params[0], poll.body.params[0]);
    assert.equal(h.nodes.job_resume.style.display, 'none');
});

test('job success words in an ordinary log cannot complete an operation', () => {
    const h = harness(); h.boot(); h.click('core_check'); h.accept(h.next('tailscale_core'));
    h.finishJob(h.next('tailscale_job'), 'running');
    h.next().finish(null, 'success\nXU6J03M6\n{"state":"success"}'); h.advance();
    assert.equal(h.nodes.apply_settings.disabled, true);
    assert.equal(h.nodes.job_state.textContent, '正在处理…');
});


test('core management remains available when structured status says the daemon is stopped', () => {
    const h = harness(); h.app.init(); h.reply(h.next(), [{ tailscale_enable: '0' }]);
    h.reply(h.next('tailscale_fettle'), h.status({ enabled: false, backend_state: 'Unavailable', online: null, error: 'local_api_unavailable' }));
    h.reply(h.next('tailscale_tsnets'), { interfaces: [] }); h.advance();
    assert.equal(h.nodes.core_update.disabled, false);
    assert.equal(h.nodes.core_rollback.disabled, false);
    assert.equal(h.nodes.tailscale_enable.checked, false);
});


test('queued save snapshots all nine settings without exposing DBus fields to httpdb', () => {
    const h = harness(); h.boot(); h.advance(5000);
    const inFlight = h.next('tailscale_fettle');
    h.change('tailscale_advertise_routes', true);
    h.change('tailscale_exit_node', true);
    h.change('tailscale_watchdog_enable', false);
    h.click('apply_settings');
    assert.equal(h.requests.length, 0);
    h.nodes.tailscale_accept_routes.checked = true;
    h.reply(inFlight, h.status());
    const save = h.next('tailscale_config');
    assert.deepEqual(save.body.fields, {});
    assert.deepEqual(save.body.params, ['web_submit', '111101000', 'b64.']);
    assert.ok(h.history.filter(r => r.body).every(r => Object.keys(r.body.fields).length === 0));
});


test('display uses reported versions and formats recovery attempts as local time', () => {
    const h = harness(); h.app.init(); h.reply(h.next(), [{}]);
    const stamp = 1790000000, date = new Date(stamp * 1000);
    const two = value => String(value).padStart(2, '0');
    h.reply(h.next('tailscale_fettle'), h.status({ plugin_version: '3.2.1',
        watchdog: { enabled: true, last_recovery: String(stamp), count_24h: 2 } }));
    assert.equal(h.nodes.plugin_version.textContent, '3.2.1');
    assert.equal(h.nodes.watchdog_recovery.textContent,
        `${date.getFullYear()}-${two(date.getMonth() + 1)}-${two(date.getDate())} ${two(date.getHours())}:${two(date.getMinutes())}:${two(date.getSeconds())}（本地时间）`);
    assert.match(h.nodes.watchdog_state.textContent, /尝试恢复 2 次/);
    h.reply(h.next('tailscale_tsnets'), { interfaces: [] }); h.advance(5000);
    h.reply(h.next('tailscale_fettle'), h.status({ plugin_version: '',
        watchdog: { enabled: false, last_recovery: '<script>bad</script>', count_24h: 0 } }));
    assert.equal(h.nodes.plugin_version.textContent, '版本未知');
    assert.equal(h.nodes.watchdog_recovery.textContent, '时间不可用');
});

test('disabled service is explained without treating an absent daemon as a connection error', () => {
    const h = harness(); h.app.init(); h.reply(h.next(), [{ tailscale_enable: '0' }]);
    h.reply(h.next('tailscale_fettle'), h.status({ enabled: false, backend_state: 'Unavailable',
        online: null, error: 'local_api_unavailable' }));
    assert.equal(h.nodes.daemon_state.textContent, '未启用');
    assert.equal(h.nodes.connection_notice.textContent, '');
    assert.equal(h.nodes.watchdog_recovery.textContent, '暂无记录');
    h.reply(h.next('tailscale_tsnets'), { interfaces: [] }); h.advance(5000);
    h.reply(h.next('tailscale_fettle'), h.status({ backend_state: 'Unavailable', error: 'local_api_unavailable' }));
    assert.equal(h.nodes.daemon_state.textContent, '暂时无法读取');
    assert.match(h.nodes.connection_notice.textContent, /诊断摘要/);
    assert.doesNotMatch(h.nodes.connection_notice.textContent, /local_api_unavailable/);
});

test('rejected settings explain the next step and keep machine phases out of user messages', () => {
    const h = harness(); h.boot(); h.click('apply_settings');
    h.reply(h.next('tailscale_config'), { accepted: false, error: 'invalid_config_snapshot' });
    assert.match(h.nodes.job_message.textContent, /刷新页面/);
    assert.doesNotMatch(h.nodes.job_message.textContent, /invalid_config_snapshot/);
    h.click('core_check'); h.accept(h.next('tailscale_core'));
    h.finishJob(h.next('tailscale_job'), 'failed', { message: '', phase: 'internal_phase' });
    assert.match(h.nodes.job_message.textContent, /操作日志/);
    assert.doesNotMatch(h.nodes.job_message.textContent, /internal_phase/);
});

// Advance ordinary polling while retaining deterministic control of the endpoint
// under test. Every write is still explicitly accepted by the test itself.
function reach(h, method) {
    for (let attempts = 0; attempts < 40; attempts++) {
        if (!h.requests.length) { h.advance(500); continue; }
        const request = h.next();
        if ((request.body && request.body.method === method) || request.settings.url === method) { return request; }
        if (!request.body && request.settings.url.startsWith('/_temp/')) { request.finish(null, 'working'); }
        else if (request.body && request.body.method === 'tailscale_job') { h.finishJob(request, 'running', { phase: 'accepted' }); }
        else if (request.body && request.body.method === 'tailscale_fettle') { h.reply(request, h.status()); }
        else if (request.body && request.body.method === 'tailscale_tsnets') { h.reply(request, { interfaces: [] }); }
        else { assert.fail('Unexpected request while awaiting ' + method + ': ' + request.settings.url); }
    }
    assert.fail('Timed out awaiting ' + method);
}
function stoppedBoot(h) {
    h.app.init(); h.reply(h.next(), [{ tailscale_enable: '0', tailscale_ipv4_enable: '1' }]);
    h.reply(h.next('tailscale_fettle'), h.status({ enabled: false, backend_state: 'Unavailable', online: null, error: 'local_api_unavailable' }));
    h.reply(h.next('tailscale_tsnets'), { interfaces: [] }); h.advance();
}
const unavailable = { enabled: true, backend_state: 'Unavailable', online: null, monitoring_available: false, error: 'local_api_unavailable' };

test('all draft options including enable are applied in one snapshot and reverting edits clears the notice', () => {
    const h = harness(); h.boot(); h.change('tailscale_enable', false);
    h.change('tailscale_enable', true);
    assert.equal(h.nodes.settings_notice.textContent, '');
    h.change('tailscale_enable', false); h.change('tailscale_exit_node', true);
    assert.equal(h.history.filter(r => r.body && r.body.method === 'tailscale_config').length, 0);
    assert.equal(h.nodes.daemon_state.textContent, '运行中');
    h.click('apply_settings');
    const write = h.next('tailscale_config');
    assert.deepEqual(write.body.params, ['web_submit', '011001100', 'b64.']);
    h.accept(write);
    assert.equal(h.nodes.settings_notice.textContent, '正在应用设置…');
    assert.equal(h.nodes.daemon_state.textContent, '正在停止…');
});

test('accepted configuration transitions show lifecycle progress and failed applies restore the draft', () => {
    for (const intent of ['start', 'stop', 'apply']) {
        const h = harness();
        if (intent === 'start') { stoppedBoot(h); h.change('tailscale_enable', true); }
        else { h.boot(); if (intent === 'stop') { h.change('tailscale_enable', false); } else { h.change('tailscale_exit_node', true); } }
        h.click('apply_settings'); h.accept(h.next('tailscale_config'));
        h.reply(reach(h, 'tailscale_fettle'), h.status(unavailable));
        assert.equal(h.nodes.connection_notice.textContent, '', intent);
        assert.equal(h.nodes.daemon_state.textContent, { start: '正在启动…', stop: '正在停止…', apply: '正在应用设置…' }[intent]);
        assert.equal(h.nodes.tailnet_state.textContent, '等待操作完成');
        assert.equal(h.nodes.settings_notice.textContent, '正在应用设置…');
        h.finishJob(reach(h, 'tailscale_job'), 'failed');
        assert.match(h.nodes.connection_notice.textContent, /诊断摘要/);
        const config = reach(h, '/_api/tailscale_');
        assert.equal(config.settings.url, '/_api/tailscale_');
        h.reply(config, [{ tailscale_enable: intent === 'start' ? '0' : '1' }]);
        assert.equal(h.nodes.tailscale_enable.checked, intent !== 'stop');
        assert.match(h.nodes.settings_notice.textContent, /尚未应用/);
    }
});

test('completed lifecycle waits for a fresh status and does not hide a continuing LocalAPI failure', () => {
    const h = harness(); stoppedBoot(h); h.change('tailscale_enable', true); h.click('apply_settings'); h.accept(h.next('tailscale_config'));
    h.reply(reach(h, 'tailscale_fettle'), h.status(unavailable));
    h.finishJob(reach(h, 'tailscale_job'), 'success');
    assert.equal(h.nodes.connection_notice.textContent, '');
    assert.equal(h.nodes.daemon_state.textContent, '正在刷新状态…');
    h.reply(reach(h, '/_api/tailscale_'), [{ tailscale_enable: '1' }]);
    h.reply(reach(h, 'tailscale_fettle'), h.status(unavailable));
    assert.match(h.nodes.connection_notice.textContent, /诊断摘要/);
    assert.equal(h.nodes.settings_notice.textContent, '');
    assert.equal(h.nodes.daemon_state.textContent, '暂时无法读取');
});

test('lifecycle progress does not hide HTTP or invalid configuration errors', () => {
    const h = harness(); stoppedBoot(h); h.change('tailscale_enable', true); h.click('apply_settings'); h.accept(h.next('tailscale_config'));
    reach(h, 'tailscale_fettle').finish('timeout');
    assert.match(h.nodes.connection_notice.textContent, /读取状态超时/);
    h.finishJob(reach(h, 'tailscale_job'), 'running');
    assert.match(h.nodes.connection_notice.textContent, /读取状态超时/);
    h.reply(reach(h, 'tailscale_fettle'), h.status({ ...unavailable, error: 'invalid_configuration' }));
    assert.match(h.nodes.connection_notice.textContent, /已保存的设置无效/);
    assert.equal(h.nodes.daemon_state.textContent, '正在启动…');
});

test('diagnostics, update checks and downloads retain LocalAPI warnings; only core switching suppresses them', () => {
    for (const button of ['run_diagnostics', 'core_check', 'core_update', 'core_rollback']) {
        const h = harness(); h.boot(); h.click(button); h.accept(h.next());
        h.finishJob(h.next('tailscale_job'), 'running', { phase: 'downloading' });
        h.reply(reach(h, 'tailscale_fettle'), h.status(unavailable));
        assert.match(h.nodes.connection_notice.textContent, /诊断摘要/, button);
        h.finishJob(reach(h, 'tailscale_job'), 'running', { phase: 'switching' });
        if (/^core_(update|rollback)$/.test(button)) {
            assert.equal(h.nodes.connection_notice.textContent, '');
            assert.equal(h.nodes.daemon_state.textContent, '正在切换核心…');
        } else { assert.match(h.nodes.connection_notice.textContent, /诊断摘要/, button); }
        h.finishJob(reach(h, 'tailscale_job'), 'rolled_back');
        assert.match(h.nodes.connection_notice.textContent, /诊断摘要/, button);
    }
});

test('reload recovers task intent and preserves a rejected draft even if intermediate settings matched it', () => {
    const first = harness(); stoppedBoot(first); first.change('tailscale_enable', true); first.click('apply_settings'); first.accept(first.next('tailscale_config'));
    const saved = JSON.parse(first.storage.tailscale3_pending);
    assert.equal(saved.method, 'tailscale_config');
    assert.equal(saved.intent, 'start');
    assert.equal(saved.targetEnabled, true);
    const h = harness({ tailscale3_pending: JSON.stringify(saved) }); h.app.init();
    h.finishJob(h.next('tailscale_job'), 'running');
    h.next().finish(null, 'starting');
    h.reply(h.next(), [{ tailscale_enable: '1', tailscale_ipv4_enable: '1' }]);
    h.reply(h.next('tailscale_fettle'), h.status(unavailable));
    assert.equal(h.nodes.daemon_state.textContent, '正在启动…');
    assert.equal(h.nodes.settings_notice.textContent, '正在应用设置…');
    h.reply(h.next('tailscale_tsnets'), { interfaces: [] });
    h.finishJob(reach(h, 'tailscale_job'), 'failed');
    h.reply(reach(h, '/_api/tailscale_'), [{ tailscale_enable: '0', tailscale_ipv4_enable: '1' }]);
    assert.equal(h.nodes.tailscale_enable.checked, true);
    assert.equal(h.nodes.settings_notice.textContent, '设置尚未应用');
    assert.equal(h.history.filter(r => r.body && r.body.method === 'tailscale_config').length, 0);
});

test('a queued Apply retains its lifecycle intent when an earlier status response arrives late', () => {
    const h = harness(); stoppedBoot(h); h.advance(5000); const stale = h.next('tailscale_fettle');
    h.change('tailscale_enable', true); h.click('apply_settings');
    assert.equal(h.nodes.settings_notice.textContent, '正在提交设置…');
    h.reply(stale, h.status(unavailable));
    assert.equal(h.nodes.daemon_state.textContent, '正在启动…');
    assert.equal(h.nodes.connection_notice.textContent, '');
    h.reply(h.next('tailscale_config'), { accepted: false, error: 'busy' });
    assert.match(h.nodes.connection_notice.textContent, /诊断摘要/);
    assert.equal(h.nodes.settings_notice.textContent, '设置尚未应用');
});

test('unconfirmed or expired jobs cannot continue suppressing service errors', () => {
    const h = harness(); stoppedBoot(h); h.change('tailscale_enable', true); h.click('apply_settings'); h.accept(h.next('tailscale_config'));
    h.reply(reach(h, 'tailscale_fettle'), h.status(unavailable));
    const held = reach(h, 'tailscale_job'); h.advance(15 * 60 * 1000); held.finish('timeout'); h.advance();
    assert.match(h.nodes.connection_notice.textContent, /诊断摘要/);
    assert.equal(h.nodes.settings_notice.textContent, '设置结果尚未确认');
    h.click('job_resume');
    assert.match(h.nodes.connection_notice.textContent, /诊断摘要/);
    assert.equal(h.nodes.settings_notice.textContent, '正在确认设置结果…');
});

test('DNS and custom routes are independent draft settings in a nine-bit submission', () => {
    const h = harness(); h.boot();
    assert.equal(h.nodes.tailscale_accept_dns.checked, false);
    assert.equal(h.nodes.custom_routes_editor.style.display, 'none');
    assert.equal(h.rows().length, 1);
    h.change('tailscale_accept_dns', true); h.change('tailscale_custom_routes_enable', true);
    assert.equal(h.nodes.tailscale_advertise_routes.checked, false);
    assert.equal(h.nodes.custom_routes_editor.style.display, '');
    h.editRoute(0, ' 192.168.60.0/24 '); h.click('custom_routes_add');
    h.editRoute(1, '2001:db8:1::/64'); h.click('custom_routes_add');
    h.editRoute(2, '   ');
    assert.equal(h.history.filter(r => r.body && r.body.method === 'tailscale_config').length, 0);
    h.click('apply_settings'); const write = h.next('tailscale_config');
    assert.deepEqual(write.body.params, ['web_submit', '111000111', wire('192.168.60.0/24,2001:db8:1::/64')]);
    assert.deepEqual(write.body.fields, {});
    assert.ok(h.rows().every(row => row.input.disabled && row.remove.disabled));
    assert.equal(h.nodes.custom_routes_add.disabled, true);
    h.accept(write);
    assert.equal(JSON.parse(h.storage.tailscale3_pending).routes, '192.168.60.0/24,2001:db8:1::/64');
});

test('route edits compare the trimmed nonempty list, preserve collapsed rows, and retain one empty row', () => {
    const h = harness(); h.boot(); h.change('tailscale_custom_routes_enable', true);
    h.editRoute(0, '192.168.60.0/24'); h.change('tailscale_custom_routes_enable', false);
    assert.equal(h.nodes.custom_routes_editor.style.display, 'none');
    assert.equal(h.rows()[0].input.value, '192.168.60.0/24');
    assert.equal(h.nodes.settings_notice.textContent, '设置尚未应用');
    h.change('tailscale_custom_routes_enable', true);
    assert.equal(h.rows()[0].input.value, '192.168.60.0/24');
    h.click('custom_routes_add'); h.editRoute(1, '2001:db8::/64'); h.removeRoute(0);
    assert.equal(h.rows()[0].input.value, '2001:db8::/64');
    h.removeRoute(0); assert.equal(h.rows().length, 1); assert.equal(h.rows()[0].input.value, '');
    h.change('tailscale_custom_routes_enable', false);
    assert.equal(h.nodes.settings_notice.textContent, '');
    h.editRoute(0, ' \t'); h.click('custom_routes_add');
    assert.equal(h.nodes.settings_notice.textContent, '');
});

test('at most 32 editable rows are available and deleting a row re-enables adding', () => {
    const h = harness(); h.boot(); h.change('tailscale_custom_routes_enable', true);
    for (let i = 1; i < 32; i++) { h.click('custom_routes_add'); }
    assert.equal(h.rows().length, 32); assert.equal(h.nodes.custom_routes_add.disabled, true);
    h.nodes.custom_routes_add.events.click(); assert.equal(h.rows().length, 32);
    h.removeRoute(10); assert.equal(h.rows().length, 31); assert.equal(h.nodes.custom_routes_add.disabled, false);
    h.click('custom_routes_add'); assert.equal(h.rows().length, 32);
});

test('format hints do not block backend validation and invalid detail is safe text on the submitted row', () => {
    const h = harness(); h.boot(); h.change('tailscale_custom_routes_enable', true);
    h.editRoute(0, '192.168.60.0/24'); h.click('custom_routes_add'); h.click('custom_routes_add');
    const unsafe = '<img src=x onerror=alert(1)>';
    h.editRoute(2, unsafe);
    assert.match(h.rows()[2].hint.textContent, /CIDR/);
    h.click('apply_settings'); const write = h.next('tailscale_config');
    assert.equal(write.body.params[2], wire('192.168.60.0/24,' + unsafe));
    const detail = '2 ' + unsafe + '：不是有效的 CIDR';
    h.reply(write, { accepted: false, error: 'invalid_custom_routes', detail }); h.advance();
    assert.equal(h.nodes.settings_notice.textContent, detail);
    assert.equal(h.nodes.job_message.textContent, detail);
    assert.equal(h.rows()[2].input.attrs['aria-invalid'], 'true');
    assert.equal(h.rows()[1].input.attrs['aria-invalid'], undefined);
    assert.equal(h.rows()[2].input.value, unsafe);
    assert.equal(h.rows()[2].hint.textContent, detail);
    assert.equal(h.nodes.apply_settings.disabled, false);
    h.editRoute(2, '119.188.240.179/32');
    assert.equal(h.rows()[2].input.attrs['aria-invalid'], undefined);
    assert.equal(h.nodes.settings_notice.textContent, '设置尚未应用');
});

test('reloading a nine-bit task restores its route draft through failure and config reload', () => {
    const saved = { id: '64531', method: 'tailscale_config', draft: '111000111', routes: '192.168.60.0/24,2001:db8::/64', intent: 'apply' };
    const h = harness({ tailscale3_pending: JSON.stringify(saved) }); h.app.init();
    h.finishJob(h.next('tailscale_job'), 'running'); h.next().finish(null, 'applying');
    h.reply(h.next(), [{ tailscale_enable: '1', tailscale_accept_dns: '0', tailscale_custom_routes_enable: '0', tailscale_custom_routes: '10.20.0.0/16' }]);
    h.reply(h.next('tailscale_fettle'), h.status()); h.reply(h.next('tailscale_tsnets'), { interfaces: [] });
    assert.equal(h.nodes.tailscale_accept_dns.checked, true);
    assert.equal(h.nodes.tailscale_custom_routes_enable.checked, true);
    assert.deepEqual(h.rows().map(row => row.input.value), ['192.168.60.0/24', '2001:db8::/64']);
    h.finishJob(reach(h, 'tailscale_job'), 'failed');
    h.reply(reach(h, '/_api/tailscale_'), [{ tailscale_enable: '1', tailscale_accept_dns: '0', tailscale_custom_routes_enable: '0', tailscale_custom_routes: '10.20.0.0/16' }]);
    assert.equal(h.nodes.tailscale_accept_dns.checked, true);
    assert.deepEqual(h.rows().map(row => row.input.value), ['192.168.60.0/24', '2001:db8::/64']);
    assert.equal(h.nodes.settings_notice.textContent, '设置尚未应用');
});

test('legacy seven-bit task drafts preserve the new configuration values read from the backend', () => {
    const saved = { id: '64532', method: 'tailscale_config', draft: '1110001', intent: 'apply' };
    const h = harness({ tailscale3_pending: JSON.stringify(saved) }); h.app.init();
    h.finishJob(h.next('tailscale_job'), 'running'); h.next().finish(null, 'applying');
    const persisted = { tailscale_enable: '0', tailscale_accept_dns: '1', tailscale_custom_routes_enable: '1', tailscale_custom_routes: '10.20.0.0/16' };
    h.reply(h.next(), [persisted]); h.reply(h.next('tailscale_fettle'), h.status()); h.reply(h.next('tailscale_tsnets'), { interfaces: [] });
    assert.equal(h.nodes.tailscale_enable.checked, true);
    assert.equal(h.nodes.tailscale_accept_dns.checked, true);
    assert.equal(h.nodes.tailscale_custom_routes_enable.checked, true);
    assert.equal(h.rows()[0].input.value, '10.20.0.0/16');
    h.finishJob(reach(h, 'tailscale_job'), 'failed'); h.reply(reach(h, '/_api/tailscale_'), [persisted]);
    assert.equal(h.nodes.tailscale_accept_dns.checked, true);
    assert.equal(h.rows()[0].input.value, '10.20.0.0/16');
    h.click('apply_settings'); const write = reach(h, 'tailscale_config');
    assert.deepEqual(write.body.params, ['web_submit', '111000111', wire('10.20.0.0/16')]);
});

test('route baseline loads saved rows and successful normalization replaces the draft', () => {
    const h = harness(); h.app.init();
    const persisted = { tailscale_custom_routes_enable: '1', tailscale_custom_routes: '2001:db8::/64' };
    h.reply(h.next(), [persisted]); h.reply(h.next('tailscale_fettle'), h.status()); h.reply(h.next('tailscale_tsnets'), { interfaces: [] }); h.advance();
    assert.equal(h.nodes.settings_notice.textContent, '');
    h.editRoute(0, ' 2001:db8::/64 '); assert.equal(h.nodes.settings_notice.textContent, '');
    h.editRoute(0, '2001:DB8::/64'); assert.equal(h.nodes.settings_notice.textContent, '设置尚未应用');
    h.click('apply_settings'); h.accept(h.next('tailscale_config')); h.finishJob(h.next('tailscale_job'), 'success');
    h.reply(reach(h, '/_api/tailscale_'), [persisted]);
    assert.equal(h.rows()[0].input.value, '2001:db8::/64'); assert.equal(h.nodes.settings_notice.textContent, '');
});

test('route status states what is advertised and primary without guessing approval', () => {
    const h = harness(); h.app.init(); h.reply(h.next(), [{}]);
    h.reply(h.next('tailscale_fettle'), h.status({ routes: { advertised: ['192.168.60.0/24', '<script>route</script>'], primary: [] } }));
    assert.equal(h.nodes.routes_advertised.textContent, '192.168.60.0/24、<script>route</script>');
    assert.equal(h.nodes.routes_primary.textContent, '无');
    assert.equal(h.nodes.routes_status.style.display, '');
    assert.doesNotMatch(h.nodes.routes_status.textContent, /待批准|未批准/);
    h.reply(h.next('tailscale_tsnets'), { interfaces: [] }); h.advance(5000);
    h.reply(h.next('tailscale_fettle'), h.status()); assert.equal(h.nodes.routes_status.style.display, 'none');
});

test('a backend error inside a pasted comma-separated row highlights the original input', () => {
    const h = harness(); h.boot(); h.change('tailscale_custom_routes_enable', true);
    h.editRoute(0, '192.168.60.0/24,192.168.60.1/24');
    h.click('apply_settings'); h.reply(h.next('tailscale_config'), { accepted: false, error: 'invalid_custom_routes', detail: '2\t192.168.60.1/24\t含主机位' });
    assert.equal(h.rows()[0].input.attrs['aria-invalid'], 'true');
    assert.equal(h.rows()[0].input.value, '192.168.60.0/24,192.168.60.1/24');
});

test('route transport uses UTF-8 base64url for invalid Unicode text and a nonempty empty-list marker', () => {
    const h = harness(); h.boot(); h.editRoute(0, '中文😀网段/32'); h.click('apply_settings');
    const write = h.next('tailscale_config');
    assert.equal(write.body.params[2], wire('中文😀网段/32'));
    h.reply(write, { accepted: false, error: 'invalid_custom_routes', detail: '1 中文😀网段/32：不是有效的 CIDR' });
    assert.match(h.nodes.settings_notice.textContent, /中文😀网段/);
    h.editRoute(0, ''); h.click('apply_settings');
    assert.equal(h.next('tailscale_config').body.params[2], 'b64.');
});

test('an oversized rejected draft is preserved for correction within the editor row limit', () => {
    const routes = Array.from({ length: 34 }, (_, index) => '192.168.' + index + '.0/24').join(',');
    const h = harness({ tailscale3_pending: JSON.stringify({ id: '64325', method: 'tailscale_config', draft: '111000111', routes }) });
    h.app.init();
    assert.equal(h.rows().length, 32);
    assert.equal(h.rows().map(row => row.input.value).join(','), routes);
});

test('httpdb explicitly rejects an oversized settings request without creating an unknown task', () => {
    const h = harness(); h.boot(); h.change('tailscale_custom_routes_enable', true);
    const draft = '1'.repeat(2048); h.editRoute(0, draft); h.click('apply_settings');
    const write = h.next('tailscale_config'); assert.equal(write.body.params[2], wire(draft));
    write.finish(null, { result: -6 }); h.advance();
    assert.equal(h.nodes.apply_settings.disabled, false);
    assert.equal(h.nodes.job_state.textContent, '操作未被接收');
    assert.match(h.nodes.settings_notice.textContent, /过长.*未接收/);
    assert.equal(h.rows()[0].input.value, draft);
    assert.equal(h.rows()[0].input.attrs['aria-invalid'], undefined);
    assert.equal(h.storage.tailscale3_pending, undefined);
    assert.equal(h.history.filter(r => r.body && r.body.method === 'tailscale_job').length, 0);
    h.editRoute(0, '192.168.60.0/24'); h.click('apply_settings');
    assert.equal(h.next('tailscale_config').body.params[2], wire('192.168.60.0/24'));
});
