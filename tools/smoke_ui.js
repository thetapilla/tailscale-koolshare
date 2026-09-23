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
let fault = '', faultUsed = false;
const calls = [], scriptErrors = [];
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
            const outbound = http.request(new URL(req.url, upstream), { method: req.method,
                headers: { 'content-type': req.headers['content-type'] || 'application/json', 'content-length': body.length } }, response => {
                const received = [];
                response.on('data', c => received.push(c));
                response.on('end', () => {
                    let data = Buffer.concat(received);
                    let status = response.statusCode;
                    const scenario = new URL(req.headers.referer || 'http://127.0.0.1').searchParams.get('scenario');
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
        console.log(JSON.stringify({ playwright: require('playwright/package.json').version, chromium: browser.version(),
            jquery: await page.evaluate(() => jQuery.fn.jquery), jquery_sha256: crypto.createHash('sha256').update(jquery).digest('hex'),
            plugin_js_sha256: crypto.createHash('sha256').update(fs.readFileSync(path.join(plugin, 'res/tailscale3.js'))).digest('hex'),
            script_url: await page.locator('script[src*="tailscale3.js"]').getAttribute('src'),
            environment: 'isolated fixture with extracted firmware userspace', real_httpdb: upstream.origin, requests: calls.length, initial_load_ms: initialLoadMs,
            result: 'passed', checks }));
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
        const initialLoadMs = Date.now() - start;
        await page.screenshot({ path: path.join(screenshots, 'loaded.png'), fullPage: true });
        console.log(JSON.stringify({ phase: 'initial state', result: 'passed', milliseconds: initialLoadMs }));
        if (args.includes('--initial-only')) { await report(['initial state'], initialLoadMs); return; }
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
        await page.evaluate(() => sessionStorage.setItem('tailscale3_pending', JSON.stringify({ id: '999999999999999', title: '应用设置', reloadConfig: true })));
        const resumeAt = calls.length;
        await page.reload(); await ready();
        assert.equal(await text('job_state'), '任务结果无法确认');
        assert.equal(await page.evaluate(() => sessionStorage.getItem('tailscale3_pending')), null);
        assert.ok(calls.slice(resumeAt).every(call => !call.method || ['tailscale_job', 'tailscale_fettle', 'tailscale_tsnets'].includes(call.method)));
        await page.screenshot({ path: path.join(screenshots, 'recovered.png'), fullPage: true });
        assert.deepEqual(scriptErrors, []);
        await report(['initial state', 'settings', 'diagnostic jobs', 'offline core check', 'malformed envelope recovery', 'login response recovery', 'HTTP failure recovery', 'missing task recovery'], initialLoadMs);
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
