'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const create = require('../plugin/res/tailscale3.js');
const html = fs.readFileSync(path.join(__dirname, '../plugin/webs/Module_tailscale.asp'), 'utf8');
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
        watchdog: { enabled: true, last_recovery: '', count_24h: 0 }, core: { installed: '1.102.4', available: '1.104.0', can_rollback: true }, ...extra }; }
    function boot() {
        app.init();
        reply(next(), [{ tailscale_enable: '1', tailscale_ipv4_enable: '1', tailscale_ipv6_enable: '1', tailscale_watchdog_enable: '1' }], true);
        reply(next('tailscale_fettle'), status());
        reply(next('tailscale_tsnets'), { interfaces: [] }, true);
        advance();
    }
    function click(id) { assert.equal(nodes[id].disabled, false, id + ' is disabled'); nodes[id].events.click(); advance(); }
    function change(id, value) { nodes[id].checked = value; nodes[id].events.change(); advance(); }
    function accept(request, string = false) { reply(request, { accepted: true, job_id: String(request.body.id) }, string); }
    function finishJob(request, state, extra = {}) { reply(request, { schema: 1, id: request.body.params[0], state, phase: 'done', message: 'Result', ...extra }); }
    return { app, nodes, requests, history, storage, confirmations, advance, next, reply, status, boot, click, change, accept, finishJob,
        maxSimultaneous: () => maxSimultaneous, timers };
}

test('native page keeps menu, all four skins, seven options, and unique HTML IDs', () => {
    const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]);
    assert.equal(ids.length, new Set(ids).size);
    for (const key of ['enable', 'ipv4_enable', 'ipv6_enable', 'advertise_routes', 'accept_routes', 'exit_node', 'watchdog_enable']) { assert.ok(ids.includes('tailscale_' + key)); }
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

test('config errors retry; empty interfaces continue polling and stale status disables core changes', () => {
    const h = harness(); h.app.init();
    h.next().finish('timeout'); h.advance(3000);
    // Other read-only polling can proceed while settings retry.
    while (h.requests[0] && h.requests[0].body) {
        const r = h.next(); h.reply(r, r.body.method === 'tailscale_fettle' ? h.status() : { interfaces: [] }); h.advance();
    }
    h.reply(h.next(), [{ tailscale_enable: '1' }]); h.advance();
    h.reply(h.next('tailscale_tsnets'), { interfaces: [] }); h.advance();
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
    assert.deepEqual(write.body.params, ['web_submit', '1110101']);
    h.reply(write, { accepted: false, error: 'busy' }); h.advance();
    assert.equal(h.nodes.apply_settings.disabled, false);
    assert.equal(h.nodes.tailscale_accept_routes.checked, true);
    assert.match(h.nodes.settings_notice.textContent, /尚未应用/);
    assert.equal(h.storage.tailscale3_pending, undefined);
});

test('switch submits config once and rapid apply cannot start another operation', () => {
    const h = harness(); h.boot(); h.change('tailscale_enable', false);
    const write = h.next('tailscale_config');
    assert.deepEqual(write.body.fields, {});
    assert.deepEqual(write.body.params, ['web_submit', '0110001']);
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
    assert.ok(ids.every(id => Number.isInteger(id) && id > 0 && id <= 1000000000));
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
    h.app.init(); h.finishJob(h.next('tailscale_job'), 'running');
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


test('queued save snapshots all seven settings without exposing DBus fields to httpdb', () => {
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
    assert.deepEqual(save.body.params, ['web_submit', '1111010']);
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
