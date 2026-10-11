#!/usr/bin/env node
/*
 * 前端真实执行测试（不是语法检查）。
 *
 * 为什么必须有这个测试：
 *   0.2.0 曾经因为重写「关于」页时误删了 app.js 末尾的启动段，导致浏览器里
 *   **界面永远停在「正在检查登录状态…」**；同一轮还丢了 installXstack /
 *   uploadGameFiles 两个函数，按钮点了没反应。
 *   `node --check` 只查语法，`curl` 只取字节 —— 两者都发现不了
 *   「函数定义了但从未被调用」「引用了不存在的函数」这类缺陷。
 *
 * 本测试的做法：
 *   1) 起一个**真实后端**（fnpack/app/server/run.py，临时沙箱目录）；
 *   2) 用一个极小的 DOM 替身把真实的 app.js 在 vm 里跑起来；
 *   3) 关键的 fetch 直接转发到真实后端（带登录 Cookie），
 *      因此每个页面渲染都是对**真实 API 响应**的验证，
 *      页面读了后端没返回的字段会立刻暴露；
 *   4) 断言：脚本可求值、注册了启动处理器、boot() 真被调用、
 *      未登录渲染登录表单、已登录渲染主界面、每个 PAGES.* 都能渲染。
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const os = require('os');
const { spawn, execFileSync } = require('child_process');
const http = require('http');

const HERE = path.resolve(__dirname, '..');
const APP_JS = process.argv[2] || path.join(HERE, 'fnpack', 'app', 'www', 'static', 'app.js');
const SERVER_DIR = path.join(HERE, 'fnpack', 'app', 'server');
const PY = process.env.PYTHON || 'python3';

let failed = 0;
const ok = (m) => console.log('  ✅ ' + m);
const bad = (m) => { console.log('  ❌ ' + m); failed++; };
const report = () => {
  console.log('');
  if (failed === 0) { console.log('== 结果：全部通过 =='); process.exit(0); }
  console.log('== 结果：失败 ' + failed + ' 项 ==');
  process.exit(1);
};

console.log('前端执行测试：' + path.relative(process.cwd(), APP_JS));

// ── 0) 真实后端 ─────────────────────────────────────────────────────────────
function freePort() {
  // listen(0) 是异步的，address() 立刻返回 null；交给 python 同步取一个空闲端口更稳
  const out = execFileSync(PY, ['-c',
    'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()'],
    { encoding: 'utf8' });
  return parseInt(out.trim(), 10);
}

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'vsfe-'));
const PORT = freePort();
const BASE = `http://127.0.0.1:${PORT}/`;
const ENV = Object.assign({}, process.env, {
  TRIM_APPDEST: path.join(TMP, 'appcenter/valleyserver'),
  TRIM_PKGVAR: path.join(TMP, 'appdata/valleyserver'),
  TRIM_PKGHOME: path.join(TMP, 'apphome/valleyserver'),
  TRIM_PKGETC: path.join(TMP, 'appconf/valleyserver'),
  TRIM_PKGTMP: path.join(TMP, 'apptemp/valleyserver'),
  TRIM_APPNAME: 'valleyserver',
  TRIM_APPVER: '0.2.0',
  APP_VERSION: '0.2.0',
  TRIM_SERVICE_PORT: String(PORT),
});
delete ENV.DATA_LIBRARY_PATH;
for (const k of ['TRIM_APPDEST', 'TRIM_PKGVAR', 'TRIM_PKGHOME', 'TRIM_PKGETC', 'TRIM_PKGTMP']) {
  fs.mkdirSync(ENV[k], { recursive: true });
}

// 生成配置与初始口令（等价于 install_callback 做的那两步）
const INIT = `
import os, sys
sys.path.insert(0, os.getcwd())
from mgr import config as C
from mgr.db import Database
C.ensure_dirs()
cfg = C.load_config()
cfg, plain = C.ensure_initial_password(cfg)
C.save_config(cfg)
Database()
print(plain)
`;
let password = '';
try {
  password = String(execFileSync(PY, ['-c', INIT], { env: ENV, cwd: SERVER_DIR, encoding: 'utf8' })).trim();
  if (!password) {
    // 兜底：从落盘的口令提示文件里解析（等价于用户实际看到的那份）
    const hint = path.join(ENV.TRIM_PKGHOME, 'initial-password.txt');
    const txt = fs.readFileSync(hint, 'utf8');
    const m = txt.match(/口令：\s*(\S+)/);
    password = m ? m[1] : '';
  }
  if (password) ok('后端沙箱初始化成功（已生成配置与初始口令）');
  else bad('未能取到初始口令');
} catch (e) {
  bad('后端沙箱初始化失败：' + e.message);
  report();
}

let server = null;
let cookie = '';
let csrf = '';
async function waitHealth(timeoutMs = 20000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    try {
      const r = await fetch(BASE + 'api/health');
      if (r.ok) return true;
    } catch (e) { /* 还没起来 */ }
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

async function startBackend() {
  server = spawn(PY, ['run.py'], { env: ENV, cwd: SERVER_DIR, stdio: ['ignore', 'pipe', 'pipe'] });
  let log = '';
  server.stdout.on('data', (d) => { log += d; });
  server.stderr.on('data', (d) => { log += d; });
  if (!await waitHealth()) {
    bad('后端未能在限定时间内就绪。输出：\n' + log.slice(-1500));
    return false;
  }
  ok('真实后端已就绪：' + BASE);
  // 登录，拿到会话 Cookie 与 CSRF
  const r = await fetch(BASE + 'api/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  });
  if (!r.ok) { bad('登录失败：HTTP ' + r.status); return false; }
  const data = await r.json();
  csrf = data.csrf || '';
  const setCookie = r.headers.get('set-cookie') || '';
  cookie = (setCookie.split(';')[0] || '').trim();
  if (!cookie) { bad('登录响应没有 Set-Cookie'); return false; }
  ok('已登录并取得会话 Cookie + CSRF');
  return true;
}

// ── 1) 极简 DOM 替身 ────────────────────────────────────────────────────────
function makeEl(tag) {
  return {
    tagName: String(tag).toUpperCase(),
    children: [], childNodes: [], attrs: {}, style: {}, dataset: {},
    classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
                 contains(c) { return this._s.has(c); }, toggle(c) { this._s.has(c) ? this._s.delete(c) : this._s.add(c); } },
    _text: '', _html: '', value: '', checked: false, disabled: false, hidden: false,
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); this.children = []; },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); this.children = []; this.childNodes = []; },
    appendChild(c) {
      if (c && c.__fragment) { (c.children || []).forEach((x) => this.appendChild(x)); return c; }
      this.children.push(c); this.childNodes.push(c);
      if (c && typeof c === 'object') c.parentNode = this;
      return c;
    },
    insertBefore(c) { return this.appendChild(c); },
    removeChild(c) { this.children = this.children.filter((x) => x !== c); return c; },
    remove() { if (this.parentNode) this.parentNode.removeChild(this); },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    removeAttribute(k) { delete this.attrs[k]; },
    addEventListener(t, fn) { (this._ev = this._ev || {})[t] = fn; },
    removeEventListener() {}, dispatchEvent() { return true; },
    focus() {}, blur() {}, click() { if (this._ev && this._ev.click) this._ev.click({ preventDefault() {} }); },
    select() {}, scrollIntoView() {}, querySelector() { return null; }, querySelectorAll() { return []; },
    closest() { return null; }, contains() { return false; },
    getBoundingClientRect() { return { width: 100, height: 100, top: 0, left: 0 }; },
  };
}

const byId = {};
const registries = { DOMContentLoaded: [], load: [], hashchange: [] };
const documentStub = {
  readyState: 'loading', _ev: {},
  createElement: (t) => makeEl(t),
  createTextNode: (t) => { const e = makeEl('#text'); e._text = String(t); return e; },
  createDocumentFragment: () => { const e = makeEl('#fragment'); e.__fragment = true; return e; },
  getElementById: (id) => (byId[id] = byId[id] || makeEl('div')),
  querySelector: (sel) => (sel && sel[0] === '#') ? (byId[sel.slice(1)] = byId[sel.slice(1)] || makeEl('div')) : makeEl('div'),
  querySelectorAll: () => [],
  addEventListener: (t, fn) => { (registries[t] = registries[t] || []).push(fn); (documentStub._ev[t] = documentStub._ev[t] || []).push(fn); },
  removeEventListener: () => {},
  body: makeEl('body'), head: makeEl('head'), documentElement: makeEl('html'), hidden: false,
};
for (const id of ['gate', 'app', 'gate-body', 'nav', 'page', 'page-title', 'page-desc', 'banner',
                  'modal-root', 'toast-root', 'task-chip', 'task-chip-text', 'task-chip-bar',
                  'brand-version', 'nav-state', 'btn-refresh', 'btn-newtab', 'btn-logout']) {
  byId[id] = makeEl(id === 'gate' || id === 'app' ? 'div' : 'div');
}

const calls = [];
// 真实后端 URL；同时把页面前缀 /app/valleyserver/ 映射到后端根
function realUrl(u) {
  const s = String(u);
  if (/^https?:/.test(s)) return s;
  const clean = s.replace(/^\/app\/valleyserver\//, '').replace(/^\/+/, '');
  return BASE + clean;
}
const fetchStub = async (url, opts) => {
  const u = String(url);
  calls.push({ url: u, opts: opts || {} });
  const init = Object.assign({}, opts || {});
  init.headers = Object.assign({}, init.headers || {});
  if (cookie) init.headers['Cookie'] = cookie;
  const method = (init.method || 'GET').toUpperCase();
  if (csrf && method !== 'GET') init.headers['X-CSRF-Token'] = csrf;
  if (init.body && typeof init.body !== 'string') init.body = String(init.body);
  return fetch(realUrl(u), init);
};

const windowStub = {
  location: { pathname: '/app/valleyserver/', search: '', hash: '#dashboard', href: BASE, reload() {} },
  addEventListener: (t, fn) => { (registries[t] = registries[t] || []).push(fn); },
  removeEventListener: () => {}, open: () => null,
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  navigator: { userAgent: 'test' },
  localStorage: { _d: {}, getItem(k) { return k in this._d ? this._d[k] : null; }, setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; } },
  document: documentStub,
  fetch: fetchStub,
};

const sandbox = {
  window: windowStub, document: documentStub, location: windowStub.location,
  navigator: windowStub.navigator, localStorage: windowStub.localStorage,
  fetch: fetchStub, Headers, Request, Response, FormData, Blob, AbortController,
  console, setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
  URL, URLSearchParams, JSON, Math, Date, Object, Array, String, Number, Boolean, Promise, Error,
  RegExp, encodeURIComponent, decodeURIComponent, parseInt, parseFloat, isNaN, isFinite,
  Intl, Map, Set, Symbol, TextEncoder, TextDecoder,
};
sandbox.globalThis = sandbox;
sandbox.self = sandbox;

// ── 2) 主流程 ───────────────────────────────────────────────────────────────
(async () => {
  if (!await startBackend()) { if (server) server.kill('SIGKILL'); report(); }

  // 2a) 用真实后端先确认健康与鉴权可用（避免把后端问题误判成前端问题）
  const health = await (await fetch(BASE + 'api/health')).json();
  if (health && (health.status === 'ok' || health.ok)) ok('后端 /api/health 正常');
  else bad('/api/health 异常：' + JSON.stringify(health).slice(0, 200));

  // 2b) 求值 app.js
  const src = fs.readFileSync(APP_JS, 'utf8');
  let ctx;
  try {
    ctx = vm.createContext(sandbox);
    vm.runInContext(src, ctx, { filename: 'app.js', timeout: 20000 });
    ok('脚本求值无异常');
  } catch (e) {
    bad('脚本求值抛异常：' + e.message);
    if (server) server.kill('SIGKILL');
    report();
  }

  // 2c) 启动处理器必须存在
  const domReady = registries.DOMContentLoaded || [];
  if (domReady.length === 0) bad('没有注册 DOMContentLoaded 处理器 —— 界面会永远停在"正在检查登录状态…"');
  else ok('已注册 DOMContentLoaded 启动处理器（' + domReady.length + ' 个）');
  if ((registries.hashchange || []).length === 0) bad('没有注册 hashchange 处理器');
  else ok('已注册 hashchange 处理器');
  if (domReady.length === 0) { if (server) server.kill('SIGKILL'); report(); }

  // 2d) 触发启动（此时 fetch 已带登录 Cookie → 走已登录分支）
  for (const fn of domReady) {
    try { await fn({}); } catch (e) { bad('启动处理器抛异常：' + e.message); }
  }
  const authCall = calls.find((c) => String(c.url).includes('auth/state'));
  if (authCall) ok('启动后请求了 api/auth/state（boot() 确实被调用）');
  else bad('启动后没有请求 api/auth/state —— boot() 未被调用');
  if (byId['app'] && byId['app'].hidden === false) ok('已登录时显示主界面（#app 可见）');
  else bad('已登录时没有显示主界面');
  const stillChecking = (byId['gate-body'].children || []).some((c) => (c.textContent || '').includes('正在检查'));
  if (!stillChecking) ok('登录门不再停留在"正在检查登录状态"占位文案');
  else bad('仍停留在"正在检查登录状态"占位文案');

  // 2e) 逐页渲染（对真实 API 响应）
  let PAGES = {};
  try { PAGES = vm.runInContext('PAGES', ctx); } catch (e) { /* ignore */ }
  const pageKeys = Object.keys(PAGES || {});
  if (pageKeys.length === 0) {
    bad('没有找到任何 PAGES.* 页面');
  } else {
    let pageFail = 0;
    for (const key of pageKeys) {
      const root = makeEl('div');
      try {
        await PAGES[key](root);
      } catch (e) {
        bad('页面 ' + key + ' 渲染抛异常：' + e.message);
        pageFail++;
      }
    }
    if (pageFail === 0) ok(pageKeys.length + ' 个页面全部渲染通过：' + pageKeys.join(', '));
  }

  // 2f) 未登录时应渲染登录表单（换一个干净的上下文）
  const sandbox2 = Object.assign({}, sandbox);
  const reg2 = { DOMContentLoaded: [] };
  const doc2 = Object.assign({}, documentStub, {
    addEventListener: (t, fn) => { (reg2[t] = reg2[t] || []).push(fn); },
    getElementById: (id) => (byId[id] = byId[id] || makeEl('div')),
  });
  const byId2 = {};
  const gateBody2 = makeEl('div');
  const fetchNoAuth = async () => ({
    ok: true, status: 200, headers: { get: () => 'application/json' },
    async json() { return { authenticated: false, via_gateway: false }; },
  });
  sandbox2.document = doc2;
  sandbox2.fetch = fetchNoAuth;
  sandbox2.window = Object.assign({}, windowStub, { document: doc2, fetch: fetchNoAuth });
  sandbox2.location = windowStub.location;
  sandbox2.globalThis = sandbox2;
  try {
    const ctx2 = vm.createContext(sandbox2);
    vm.runInContext(src.replace(/const PAGES = \{\};/g, 'const PAGES = {};'), ctx2, { filename: 'app.js', timeout: 20000 });
    for (const fn of (reg2.DOMContentLoaded || [])) await fn({});
    const gate2 = (() => { try { return vm.runInContext('document.getElementById("gate-body")', ctx2); } catch (e) { return null; } })();
    const kids = (gate2 && gate2.children) || [];
    const hasInput = kids.some((c) => c.tagName === 'INPUT');
    const hasBtn = kids.some((c) => c.tagName === 'BUTTON');
    if (hasInput && hasBtn) ok('未登录时渲染出登录输入框与按钮');
    else bad('未登录时没有渲染登录表单（子节点数=' + kids.length + '）');
  } catch (e) {
    bad('未登录分支测试抛异常：' + e.message);
  }

  // 2g) 飞牛统一网关场景：用 X-Trim-* 可信头（而不是 Cookie）必须直接进入主界面。
  //     用户实际就是从应用中心（网关 iframe）打开的，这条路径必须单独验证。
  {
    const registries3 = {};
    const byId3 = {};
    const document3 = Object.assign({}, documentStub, {
      addEventListener: (tt, fn) => { (registries3[tt] = registries3[tt] || []).push(fn); },
      getElementById: (id) => (byId3[id] = byId3[id] || makeEl('div')),
      querySelector: (sel) => (sel && sel[0] === '#') ? (byId3[sel.slice(1)] = byId3[sel.slice(1)] || makeEl('div')) : makeEl('div'),
    });
    // 经 Unix 套接字 + 网关注入头 —— 与 fnOS 网关的真实行为一致
    // （app/ui/config 里声明 gatewaySocket=valleyserver.sock）。
    // 注意不能用 TCP 伪造这些头：那正是被修复掉的认证绕过，现在会被拒。
    const sockPath = path.join(ENV.TRIM_APPDEST, 'valleyserver.sock');
    const udsFetch = (url, opts) => new Promise((resolve, reject) => {
      const u = String(url);
      const clean = u.replace(/^\/app\/valleyserver\//, '').replace(/^\/+/, '');
      const init = Object.assign({}, opts || {});
      const headers = Object.assign({
        'X-Trim-Userid': '1000', 'X-Trim-Username': 'admin', 'X-Trim-Isadmin': 'true',
      }, init.headers || {});
      let payload = null;
      if (init.body !== undefined && init.body !== null) {
        payload = typeof init.body === 'string' ? init.body : String(init.body);
        headers['Content-Length'] = Buffer.byteLength(payload);
      }
      const req = http.request({
        socketPath: sockPath,
        path: '/app/valleyserver/' + clean,
        method: (init.method || 'GET').toUpperCase(),
        headers,
      }, (res) => {
        const chunks = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8');
          resolve({
            ok: res.statusCode >= 200 && res.statusCode < 400,
            status: res.statusCode,
            headers: { get: (k) => res.headers[String(k).toLowerCase()] || null },
            async text() { return text; },
            async json() { return JSON.parse(text); },
          });
        });
      });
      req.on('error', reject);
      if (payload !== null) req.write(payload);
      req.end();
    });
    const fetchGateway = udsFetch;
    const sandbox3 = Object.assign({}, sandbox, {
      document: document3, fetch: fetchGateway,
      window: Object.assign({}, windowStub, { document: document3, fetch: fetchGateway }),
      globalThis: null,
    });
    sandbox3.globalThis = sandbox3;
    try {
      const ctx3 = vm.createContext(sandbox3);
      vm.runInContext(src, ctx3, { filename: 'app.js', timeout: 20000 });
      for (const fn of (registries3.DOMContentLoaded || [])) await fn({});
      const app3 = byId3['app'];
      const gate3kids = (byId3['gate-body'] || {}).children || [];
      if (app3 && app3.hidden === false && gate3kids.length === 0) {
        ok('经飞牛网关（X-Trim-* 头）直接进入主界面，未要求登录');
      } else {
        bad('网关场景未直接进入主界面（#app.hidden=' + (app3 && app3.hidden) + '，登录门子节点=' + gate3kids.length + '）');
      }
      // 网关场景下不应显示"退出"按钮（退出由飞牛应用中心管理）
      const logoutBtn = byId3['btn-logout'];
      if (logoutBtn && logoutBtn.hidden === true) ok('网关场景隐藏「退出」按钮（由飞牛应用中心管理会话）');
      else bad('网关场景未正确隐藏「退出」按钮');
    } catch (e) {
      bad('网关场景测试抛异常：' + e.message);
    }
  }

  if (server) { server.kill('SIGKILL'); }
  try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* ignore */ }
  report();
})();
