#!/usr/bin/env node
'use strict';
// Browser contract check against an isolated real httpdb served by smoke_httpdb.py.
// Firmware assets remain local inputs and are never copied into the repository.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const crypto = require('node:crypto');
const { chromium } = require('playwright');
const args = process.argv.slice(2);
function option(name, fallback) {
    const at = args.indexOf(name);
    return at < 0 ? fallback : args[at + 1];
}
const root = path.resolve(__dirname, '..');
const version = fs.readFileSync(path.join(root, 'VERSION'), 'utf8').trim();
const plugin = path.resolve(option('--plugin-root', path.join(root, 'plugin')));
const screenshots = path.resolve(option('--screenshots', path.join(root, 'build/ui-smoke')));
fs.mkdirSync(screenshots, { recursive: true });
const firmware = option('--firmware-root');
const upstream = new URL(option('--upstream', 'http://127.0.0.1:33030'));
if (!firmware || !fs.existsSync(path.join(firmware, 'www/js/jquery.js'))) {
    throw new Error('--firmware-root must point to an extracted firmware root with www/js/jquery.js');
}
if (!['127.0.0.1', 'localhost', '[::1]'].includes(upstream.hostname)) {
    throw new Error('The test backend must be a loopback address');
}
const jquery = fs.readFileSync(path.join(firmware, 'www/js/jquery.js'));
const html = fs.readFileSync(path.join(plugin, 'webs/Module_tailscale.asp'), 'utf8')
    .replace(/<%\s*nvram_get\("sc_skin"\);\s*%>/g, 'ASUSWRT');
// A minimal Shell/httpdb export is insufficient for visual interaction tests.
// Missing styles can hide the switch while still allowing JavaScript tests to run.
for (const match of html.matchAll(/href="(\/[^"?]+\.css)"/g)) {
    const asset = path.join(firmware, match[1].startsWith('/res/') ? 'rom/etc/koolshare' : 'www', match[1]);
    if (!fs.existsSync(asset) || !fs.statSync(asset).size) {
        throw new Error('Missing required page stylesheet; use the full extracted firmware root: ' + match[1]);
    }
}
let fault = '', faultUsed = false;
const calls = [], scriptErrors = [];
// Delayed lifecycle responses isolate the browser's transition semantics. The
// ordinary scenarios below still exercise the extracted firmware's real httpdb.
const lifecycle = { config: { tailscale_enable: '0', tailscale_ipv4_enable: '1', tailscale_ipv6_enable: '1',
    tailscale_advertise_routes: '0', tailscale_accept_routes: '0', tailscale_exit_node: '0', tailscale_watchdog_enable: '0' },
    task: null, failNext: false, unavailableReads: 0 };
// Status overlays cover signed-descriptor identities and delayed authorization
// without downloading cores or registering a device on a real tailnet.
const identity = { enabled: true, core_version: '1.104.1', auth_url: '', core: {
    installed: '1.104.1', installed_build: 'r1', available: '1.104.1', available_build: 'r2',
    update_available: true, can_rollback: true, previous: '1.102.4', previous_build: 'legacy'
} };
function lifecycleResponse(url, body) {
    if (url.pathname === '/_api/tailscale_') { return { result: [lifecycle.config] }; }
    const call = JSON.parse(body.toString());
    const task = lifecycle.task;
    let data;
    if (call.method === 'tailscale_config') {
        lifecycle.task = { id: String(call.id), started: Date.now(), bits: call.params[1], fail: lifecycle.failNext, state: 'running' };
        lifecycle.failNext = false;
        data = { accepted: true, job_id: String(call.id) };
    } else if (call.method === 'tailscale_job') {
        if (task && Date.now() - task.started >= 8500) {
            task.state = task.fail ? 'failed' : 'success';
            if (!task.fail) { Object.keys(lifecycle.config).forEach((key, index) => { lifecycle.config[key] = task.bits[index]; }); }
        }
        data = { schema: 1, id: task.id, state: task.state, phase: task.state === 'running' ? 'accepted' : 'complete',
            message: task.fail && task.state === 'failed' ? '服务启动失败' : '' };
    } else if (call.method === 'tailscale_fettle') {
        const changing = task && task.state === 'running';
        const unavailable = changing || (task && task.state === 'failed');
        if (changing) { lifecycle.unavailableReads++; }
        data = { schema: 1, plugin_version: version, core_version: '1.104.1', enabled: unavailable ? true : lifecycle.config.tailscale_enable === '1',
            backend_state: unavailable ? 'Unavailable' : 'Running', online: unavailable ? null : true,
            monitoring_available: !unavailable, error: unavailable ? 'local_api_unavailable' : '',
            core: { installed: '1.104.1' }, watchdog: { enabled: false, count_24h: 0 }, health_messages: [] };
    } else if (call.method === 'tailscale_tsnets') { data = { interfaces: [] }; }
    else { throw new Error('Unexpected lifecycle request: ' + call.method); }
    return { result: JSON.stringify(data) };
}
const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1');
    if (url.pathname.startsWith('/_api/') || url.pathname.startsWith('/_temp/')) {
        const chunks = [];
        req.on('data', c => chunks.push(c));
        req.on('end', () => {
            const body = Buffer.concat(chunks);
            let method = '';
            try { method = JSON.parse(body.toString()).method; } catch (_) { /* GET */ }
            calls.push({ method, path: req.url, contentType: req.headers['content-type'] });
            const scenario = new URL(req.headers.referer || 'http://127.0.0.1').searchParams.get('scenario');
            if (scenario === 'lifecycle') {
                res.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
                res.end(url.pathname.startsWith('/_temp/') ? 'fixture task log' : JSON.stringify(lifecycleResponse(url, body)));
                return;
            }
            const outbound = http.request(new URL(req.url, upstream), { method: req.method,
                headers: { 'content-type': req.headers['content-type'] || 'application/json', 'content-length': body.length } }, response => {
                const received = [];
                response.on('data', c => received.push(c));
                response.on('end', () => {
                    let data = Buffer.concat(received);
                    let status = response.statusCode;
                    const scenario = new URL(req.headers.referer || 'http://127.0.0.1').searchParams.get('scenario');
                    if (scenario === 'identity' && method === 'tailscale_fettle') {
                        const wrapped = JSON.parse(data.toString());
                        const statusData = JSON.parse(wrapped.result);
                        Object.assign(statusData, identity);
                        wrapped.result = JSON.stringify(statusData);
                        data = Buffer.from(JSON.stringify(wrapped));
                    }
                    if (fault && scenario === fault && !faultUsed && method === 'tailscale_fettle') {
                        faultUsed = true;
                        if (fault === 'malformed') {
                            const wrapped = JSON.parse(data.toString());
                            // Reproduce the legacy unescaped response envelope at the HTTP boundary.
                            data = Buffer.from('{"result": "' + wrapped.result + '"}');
                        } else if (fault === 'login') { data = Buffer.from('<html><body>Sign in</body></html>'); }
                        else { status = 503; data = Buffer.from('Unavailable'); }
                    }
                    res.writeHead(status, { 'Content-Type': url.pathname.startsWith('/_api/') ? 'application/json' : 'text/plain', 'Cache-Control': 'no-store' });
                    res.end(data);
                });
            });
            outbound.on('error', error => { res.writeHead(502); res.end(String(error)); });
            outbound.end(body);
        });
        return;
    }
    let data = '', contentType = 'application/javascript';
    if (url.pathname === '/' || url.pathname === '/Module_tailscale.asp') { data = html; contentType = 'text/html; charset=utf-8'; }
    else if (url.pathname === '/js/jquery.js') { data = jquery; }
    else if (url.pathname === '/res/tailscale3.js') { data = fs.readFileSync(path.join(plugin, 'res/tailscale3.js')); }
    else if (url.pathname === '/res/softcenter.js') { data = fs.readFileSync(path.join(firmware, 'rom/etc/koolshare/res/softcenter.js')); }
    else if (url.pathname === '/state.js') {
        // Router navigation is outside this plugin. Preserve the plugin init/menu hook call.
        data = 'var tabtitle=[[]],tablink=[[]];function show_menu(hook){hook();}';
    } else if (/\.(css|png|ico|gif|jpg|jpeg|svg|woff|woff2|ttf|eot)$/.test(url.pathname)) {
        const base = path.resolve(firmware, url.pathname.startsWith('/res/') ? 'rom/etc/koolshare' : 'www');
        const asset = path.resolve(base, '.' + url.pathname);
        if (asset.startsWith(base + path.sep) && fs.existsSync(asset) && fs.statSync(asset).isFile()) { data = fs.readFileSync(asset); }
        const own = path.resolve(plugin, '.' + url.pathname);
        if (url.pathname.startsWith('/res/') && own.startsWith(plugin + path.sep) && fs.existsSync(own) && fs.statSync(own).isFile()) { data = fs.readFileSync(own); }
        const mime = { css: 'text/css', png: 'image/png', ico: 'image/x-icon', gif: 'image/gif', jpg: 'image/jpeg', jpeg: 'image/jpeg',
            svg: 'image/svg+xml', woff: 'font/woff', woff2: 'font/woff2', ttf: 'font/ttf', eot: 'application/vnd.ms-fontobject' };
        contentType = mime[path.extname(url.pathname).slice(1)];
    }
    res.writeHead(200, { 'Content-Type': contentType, 'Cache-Control': 'no-store' });
    res.end(data);
});

(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const url = 'http://127.0.0.1:' + server.address().port + '/Module_tailscale.asp';
    const browser = await chromium.launch({ headless: true, executablePath: option('--chromium') });
    const context = await browser.newContext();
    await context.route('**/*', route => {
        const target = new URL(route.request().url());
        return target.hostname === '127.0.0.1' ? route.continue() : route.abort();
    });
    const page = await context.newPage();
    page.on('pageerror', e => scriptErrors.push(e.message));
    page.on('dialog', dialog => dialog.accept());
    const waitEnabled = id => page.waitForFunction(id => !document.getElementById(id).disabled, id, { timeout: 25000 });
    const text = id => page.locator('#' + id).textContent();
    async function ready() {
        await page.waitForFunction(version => document.getElementById('plugin_version').textContent === version, version, { timeout: 20000 });
        await waitEnabled('apply_settings');
        for (const id of ['plugin_version', 'core_current', 'daemon_state', 'tailnet_state', 'monitoring_state', 'watchdog_recovery']) {
            assert.doesNotMatch(await text(id), /正在读取|暂时无法读取/, id);
        }
        await page.waitForFunction(() => document.getElementById('interfaces_body').children.length > 0 ||
            /暂无|暂时无法读取/.test(document.getElementById('interfaces_notice').textContent), null, { timeout: 15000 });
    }
    async function report(checks, initialLoadMs) {
        const result = { playwright: require('playwright/package.json').version, chromium: browser.version(),
            jquery: await page.evaluate(() => jQuery.fn.jquery), jquery_sha256: crypto.createHash('sha256').update(jquery).digest('hex'),
            plugin_js_sha256: crypto.createHash('sha256').update(fs.readFileSync(path.join(plugin, 'res/tailscale3.js'))).digest('hex'),
            script_url: await page.locator('script[src*="tailscale3.js"]').getAttribute('src'),
            environment: 'isolated fixture with extracted firmware userspace', real_httpdb: upstream.origin, requests: calls.length, initial_load_ms: initialLoadMs,
            lifecycle_transport: checks.includes('delayed start/stop status') ? 'deterministic delayed HTTP responses at local proxy' : 'not exercised',
            identity_transport: checks.includes('core build identities') ? 'status overlays on real HTTP/DBus replies at local proxy' : 'not exercised',
            result: 'passed', checks };
        fs.writeFileSync(path.join(screenshots, 'result.json'), JSON.stringify(result, null, 2) + '\n');
        console.log(JSON.stringify(result));
    }
    async function job(button, expected) {
        await waitEnabled(button);
        const before = calls.length;
        await page.locator('#' + button).click();
        await page.waitForFunction(() => /^(操作完成|操作失败|核心切换未完成)/.test(document.getElementById('job_state').textContent), null, { timeout: 35000 });
        const state = await text('job_state');
        assert.match(state, expected);
        await waitEnabled('apply_settings');
        assert.ok(calls.slice(before).some(call => call.method === 'tailscale_job'), 'Job completion must come from its status endpoint');
        assert.ok(await text('job_id'));
        return state;
    }
    try {
        const start = Date.now();
        await page.goto(url);
        await ready();
        assert.match(await page.evaluate(() => jQuery.fn.jquery), /^(1\.10\.2|3\.7\.1)$/);
        await page.waitForFunction(() => document.getElementById('interfaces_notice').textContent !== '正在读取…' &&
            (document.getElementById('interfaces_body').children.length > 0 || document.getElementById('interfaces_notice').textContent.includes('暂无')), null, { timeout: 15000 });
        const headingColors = await page.locator('#tailscale_tcnets th').evaluateAll(elements => elements.map(element => getComputedStyle(element).color));
        assert.equal(headingColors.length, 4);
        assert.ok(headingColors.every(color => color === 'rgb(241, 243, 244)'), 'Network table headings must retain readable contrast');
        assert.deepEqual(scriptErrors, []);
        const initialLoadMs = Date.now() - start;
        await page.screenshot({ path: path.join(screenshots, 'loaded.png'), fullPage: true });
        console.log(JSON.stringify({ phase: 'initial state', result: 'passed', milliseconds: initialLoadMs }));
        if (args.includes('--initial-only')) { await report(['initial state', 'table heading contrast'], initialLoadMs); return; }
        await job('run_diagnostics', /^操作完成$/);
        await job('run_status', /^操作完成$/);
        await job('run_netcheck', /^操作完成$/);
        await page.locator('#tailscale_accept_routes').uncheck();
        await job('apply_settings', /^操作完成$/);
        await page.waitForFunction(() => !document.getElementById('tailscale_accept_routes').checked && !document.getElementById('apply_settings').disabled);
        await job('core_check', /^(操作完成|操作失败)$/);
        assert.equal(await page.locator('#core_update').isDisabled(), true, 'An offline fixture must not offer an unverified update');
        const required = ['tailscale_fettle', 'tailscale_tsnets', 'tailscale_diagnostics', 'tailscale_status', 'tailscale_ncheck', 'tailscale_config', 'tailscale_core', 'tailscale_job'];
        for (const method of required) { assert.ok(calls.some(call => call.method === method), method); }
        console.log(JSON.stringify({ phase: 'settings and task flows', result: 'passed' }));
        for (const kind of ['malformed', 'login', 'unavailable']) {
            fault = kind; faultUsed = false;
            await page.goto(url + '?scenario=' + kind);
            await page.waitForFunction(() => document.getElementById('daemon_state').textContent === '暂时无法读取', null, { timeout: 15000 });
            assert.doesNotMatch(await text('core_current'), /正在读取/);
            assert.match(await text('connection_notice'), /重试/);
            if (kind === 'malformed') { await page.screenshot({ path: path.join(screenshots, 'read-error.png'), fullPage: true }); }
            await ready();
            console.log(JSON.stringify({ phase: kind + ' response recovery', result: 'passed' }));
        }
        fault = '';
        await page.goto(url + '?scenario=lifecycle'); await ready();
        async function lifecycleApply(enabled, fail) {
            await waitEnabled('apply_settings');
            const before = calls.length;
            if (await page.locator('#tailscale_enable').isChecked() !== enabled) { await page.locator('.switch_container').click(); }
            assert.equal(await page.locator('#tailscale_enable').isChecked(), enabled);
            assert.equal(await text('settings_notice'), '设置尚未应用');
            assert.equal(calls.slice(before).filter(call => call.method === 'tailscale_config').length, 0);
            lifecycle.failNext = fail;
            const reads = lifecycle.unavailableReads;
            await page.locator('#apply_settings').click();
            await page.waitForFunction(() => document.getElementById('settings_notice').textContent === '正在应用设置…');
            assert.equal(await text('daemon_state'), enabled ? '正在启动…' : '正在停止…');
            const until = Date.now() + 12000;
            while (lifecycle.unavailableReads === reads && Date.now() < until) { await new Promise(resolve => setTimeout(resolve, 50)); }
            assert.ok(lifecycle.unavailableReads > reads, 'The browser must receive the injected missing-LocalAPI response during the transition');
            await page.waitForFunction(() => document.getElementById('tailnet_state').textContent === '等待操作完成');
            assert.equal(await text('connection_notice'), '');
            assert.doesNotMatch(await text('settings_notice'), /尚未应用/);
            await page.screenshot({ path: path.join(screenshots, fail ? 'starting-failure.png' : enabled ? 'starting.png' : 'stopping.png'), fullPage: true });
            await page.waitForFunction(() => /^(操作完成|操作失败)$/.test(document.getElementById('job_state').textContent), null, { timeout: 20000 });
            await waitEnabled('apply_settings');
            assert.equal(await text('job_state'), fail ? '操作失败' : '操作完成');
            assert.equal(calls.slice(before).filter(call => call.method === 'tailscale_config').length, 1);
            if (fail) {
                await page.waitForFunction(() => document.getElementById('connection_notice').textContent.includes('诊断摘要'));
                assert.equal(await text('settings_notice'), '设置尚未应用');
                assert.equal(await page.locator('#tailscale_enable').isChecked(), enabled);
            } else { assert.equal(await text('settings_notice'), ''); }
        }
        await lifecycleApply(true, false);
        await lifecycleApply(false, false);
        await lifecycleApply(true, true);
        console.log(JSON.stringify({ phase: 'delayed lifecycle responses: start, stop, failed start', result: 'passed' }));
        const identityAt = calls.length;
        await page.goto(url + '?scenario=identity'); await ready();
        assert.equal(await text('core_current'), '1.104.1 (r1)');
        assert.equal(await text('core_latest'), '1.104.1 (r2)');
        assert.equal(await page.locator('#core_update').isDisabled(), false);
        assert.equal(await text('core_rollback_target'), '将回退到 1.102.4（原版核心）');
        identity.core.installed_build = 'r2';
        identity.core.update_available = false;
        identity.core.previous = '1.104.1';
        identity.core.previous_build = 'r1';
        await page.waitForFunction(() => document.getElementById('core_current').textContent === '1.104.1 (r2)');
        assert.equal(await text('core_latest'), '1.104.1 (r2)（与当前核心相同）');
        assert.equal(await page.locator('#core_update').isDisabled(), true);
        assert.equal(await text('core_rollback_target'), '将回退到 1.104.1 (r1)');
        await page.screenshot({ path: path.join(screenshots, 'core-builds.png'), fullPage: true });
        identity.core_version = '1.102.4';
        await page.waitForFunction(() => document.getElementById('core_current').textContent === '1.102.4');
        identity.core.installed = '1.102.4';
        identity.core.installed_build = 'legacy';
        identity.core.update_available = true;
        await page.waitForFunction(() => document.getElementById('core_current').textContent === '1.102.4（原版核心）');
        assert.equal(await page.locator('#core_update').isDisabled(), false);
        assert.equal(await page.locator('#auth_link').isVisible(), false);
        identity.auth_url = 'https://login.tailscale.com/a/delayed-browser-test';
        identity.backend_state = 'NeedsLogin';
        identity.online = false;
        await page.locator('#auth_link').waitFor({ state: 'visible', timeout: 15000 });
        assert.equal(await page.locator('#auth_link').getAttribute('href'), identity.auth_url);
        assert.ok(calls.slice(identityAt).every(call => !call.method || ['tailscale_fettle', 'tailscale_tsnets'].includes(call.method)),
            'Identity changes and authorization links must arrive through read-only polling');
        console.log(JSON.stringify({ phase: 'core build identities, update eligibility and delayed authorization', result: 'passed' }));
        await page.goto(url); await ready();
        await page.evaluate(() => sessionStorage.setItem('tailscale3_pending', JSON.stringify({ id: '999999999999999', title: '应用设置', reloadConfig: true })));
        const resumeAt = calls.length;
        await page.reload(); await ready();
        assert.equal(await text('job_state'), '任务结果无法确认');
        assert.equal(await page.evaluate(() => sessionStorage.getItem('tailscale3_pending')), null);
        assert.ok(calls.slice(resumeAt).every(call => !call.method || ['tailscale_job', 'tailscale_fettle', 'tailscale_tsnets'].includes(call.method)));
        await page.screenshot({ path: path.join(screenshots, 'recovered.png'), fullPage: true });
        assert.deepEqual(scriptErrors, []);
        await report(['initial state', 'settings', 'diagnostic jobs', 'offline core check', 'malformed envelope recovery', 'login response recovery', 'HTTP failure recovery', 'missing task recovery', 'draft-only enable switch', 'delayed start/stop status', 'failed apply draft recovery', 'core build identities', 'backend update eligibility', 'delayed authorization link'], initialLoadMs);
    } catch (error) {
        await page.screenshot({ path: path.join(screenshots, 'failure.png'), fullPage: true }).catch(() => {});
        console.error(JSON.stringify({ script_errors: scriptErrors, requests: calls, page_state: await page.evaluate(() => ({
            version: document.getElementById('plugin_version').textContent,
            notice: document.getElementById('connection_notice').textContent,
            settings: document.getElementById('settings_notice').textContent,
            task: document.getElementById('job_state').textContent
        })) }));
        throw error;
    } finally {
        await context.close(); await browser.close(); await new Promise(resolve => server.close(resolve));
    }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
