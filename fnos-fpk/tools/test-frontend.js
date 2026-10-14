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

  // 2d-2) 路由层：新五入口 + 旧路由兼容映射（需求 §3.1/§3.2）
  try {
    const run = (expr) => vm.runInContext(expr, ctx);
    const cases = [
      ['', 'dashboard', '空 hash → 总览'],
      ['#dashboard', 'dashboard', '总览原样保留'],
      ['#console', 'game/runtime', '旧 #console → 游戏管理 › 运行与日志'],
      ['#saves', 'game/saves', '旧 #saves → 游戏管理 › 存档'],
      ['#mods', 'game/mods', '旧 #mods → 游戏管理 › 模组'],
      ['#snapshots', 'backup/snapshots', '旧 #snapshots → 备份与恢复'],
      ['#versions', 'settings/version', '旧 #versions → 设置与维护 › 版本管理'],
      ['#config', 'settings/runtime', '旧 #config → 设置与维护 › 运行设置'],
      ['#system', 'settings/environment', '旧 #system → 设置与维护 › 游戏与运行环境'],
      ['#about', 'about', '关于保持不变'],
      ['#game', 'game/runtime', '一级入口 → 默认二级'],
      ['#settings/display', 'settings/display', '二级页直达'],
      ['#tasks/queue', 'tasks/queue', '二级页直达'],
      ['#settings/nope', 'settings/runtime', '未知二级 → 回落到默认二级（不死循环）'],
      ['#nonsense', 'dashboard', '未知一级 → 总览'],
    ];
    let routeFail = 0;
    for (const [hash, want, why] of cases) {
      const got = run(`parseRoute(${JSON.stringify(hash)}).key`);
      if (got !== want) { bad(`路由 ${why}：期望 ${want}，实际 ${got}`); routeFail++; }
    }
    if (routeFail === 0) ok(`路由层 15 条映射全部正确（含旧路由兼容与未知回落）`);

    const navCount = run('NAV_MAIN.length');
    const footCount = run('NAV_FOOT.length');
    if (navCount === 5 && footCount === 1) ok('一级导航为 5 个入口 + 侧栏底部「关于」');
    else bad(`一级导航数量不对：main=${navCount} foot=${footCount}`);

    const secCount = run('Object.values(SECTIONS).reduce((n, s) => n + s.length, 0)');
    if (secCount >= 14) ok(`二级标签共 ${secCount} 个（按主题分区）`);
    else bad(`二级标签过少：${secCount}`);

    // 状态模型必须两个维度都渲染（需求 §5）
    const hasService = run('typeof stateBadge') === 'function';
    const svc = run('JSON.stringify(Object.keys(SERVICE_STATE_STYLE))');
    const farm = run('JSON.stringify(Object.keys(FARM_STATE_STYLE))');
    if (hasService && svc.includes('启动失败') && farm.includes('尚未创建存档')
        && farm.includes('农场已载入')) {
      ok('双状态模型齐备：服务运行状态与服务农场开服状态分开表达');
    } else bad('双状态模型缺失或状态不全');

    // 表格在手机端要能卡片化：必须带 responsive 类与 data-label
    // 注：测试用的假 DOM 不支持 querySelector，这里按 children 逐层取。
    const tbl = run(`(function(){
      const rows = [el('tr', {}, [el('td', { text: 'a' }), el('td', { text: 'b' })])];
      const box = table(['列一', '列二'], rows);
      const t = box.children[0];
      const tb = t.children[1];
      const firstRow = tb.children[0];
      const td = firstRow.children[0];
      return JSON.stringify({ cls: t.className, label: td.getAttribute('data-label') });
    })()`);
    if (tbl.includes('responsive') && tbl.includes('列一')) {
      ok('表格带 responsive 类与 data-label（手机端可卡片化）');
    } else bad('表格缺少手机端卡片化所需的类或 data-label');
  } catch (e) {
    bad('路由层断言执行失败：' + e.message);
  }

  // 2d-3) 响应式三档（需求 §8）与安全确认（需求 §7）
  try {
    const fs = require('fs');
    const path = require('path');
    const css = fs.readFileSync(path.join(__dirname, '..', 'fnpack/app/www/static/style.css'), 'utf8');
    // 不只检查"媒体查询存在"，还要检查**断点内部的规则**真的写了 ——
    // 否则把手机端布局整段删掉、只留一个空的 @media 也能骗过测试。
    const blockOf = (maxWidth) => {
      const re = new RegExp('@media\\s*\\(max-width:\\s*' + maxWidth + 'px\\)\\s*\\{');
      const m = re.exec(css);
      if (!m) return null;
      let i = m.index + m[0].length; let depth = 1; const start = i;
      while (i < css.length && depth > 0) {
        if (css[i] === '{') depth++;
        else if (css[i] === '}') depth--;
        i++;
      }
      return css.slice(start, i - 1);
    };
    const tablet = blockOf(1100);
    const phone = blockOf(720);
    const need = [
      [tablet, /#nav a span\.label[^{]*\{[^}]*display:\s*none/, '平板：导航收起文字'],
      [tablet, /\.grid\.c3[^{]*\{/, '平板：卡片列数减少'],
      [tablet, /\.two-col[^{]*\{[^}]*grid-template-columns:\s*1fr/, '平板：两列折成单列'],
      [phone, /#app\s*\{[^}]*display:\s*block/, '手机：主区改块级布局'],
      [phone, /\.sidebar[^{]*\{[^}]*flex-direction:\s*row/, '手机：侧栏变顶部横向导航'],
      [phone, /#nav a span\.label[^{]*\{[^}]*display:\s*inline/, '手机：导航显示文字'],
      [phone, /table\.responsive thead[^{]*\{[^}]*display:\s*none/, '手机：表头隐藏'],
      [phone, /table\.responsive td::before[^{]*\{/, '手机：单元格显示字段名'],
      [phone, /overflow-wrap:\s*anywhere/, '手机：长路径可换行'],
      [phone, /\.btn-row[^{]*\{[^}]*flex-wrap:\s*wrap/, '手机：按钮自动换行'],
    ];
    let cssFail = 0;
    if (!tablet || !phone) { bad('缺少响应式断点（平板 1100 / 手机 720）'); cssFail++; }
    for (const [blk, re, why] of need) {
      if (!blk || !re.test(blk)) { bad('断点内缺少规则：' + why); cssFail++; }
    }
    if (cssFail === 0) ok('响应式三档断点及其内部规则齐备（平板 3 条 + 手机 7 条断言）');
    const hooks = [
      ['subtabs', '二级标签'], ['state-badge', '状态徽章'],
      ['table.responsive', '表格卡片化'], ['data-label', '卡片字段名'],
      ['overflow-wrap', '长路径换行'],
    ];
    const missing = hooks.filter(([k]) => !css.includes(k)).map(([, n]) => n);
    if (missing.length === 0) ok('移动端所需样式钩子齐备（二级标签/状态徽章/表格卡片化/长文本换行）');
    else bad('缺少移动端样式钩子：' + missing.join('、'));

    // 安全确认与不可破坏的约束仍在
    const js = fs.readFileSync(path.join(__dirname, '..', 'fnpack/app/www/static/app.js'), 'utf8');
    const guards = [
      [/confirmRisk/, '危险操作确认 confirmRisk'],
      [/requireText/, '破坏性操作需输入对象名'],
      [/执行前会自动创建快照/, '风险提示写明自动快照'],
      [/需手动新建存档并重启服务端/, '无存档时的手动引导'],
      [/WebVNC 无认证、VNC 用 8 位随机密码/, '无认证风险提示'],
      [/onclick: \(\) => \{ hostBootstrap\(\); \}/, '手动载入存档并开服按钮必须真的接上 hostBootstrap()'],
      [/未能自动载入农场/, '自动载档失败时要给出显眼提示'],
      [/手动载入最新存档并开服/, '未载入农场时的手动载入按钮'],
      [/手动载入存档并开服/, '快捷操作里的手动载入按钮'],
      [/农场已载入（暂停状态）/, '暂停状态徽章样式'],
      [/const farmLoaded = !!\(ready\.farm \|\| \{\}\)\.hosting/, '未载入横幅按 hosting 判断'],
      [/玩家全部退出时自动备份/, '无人在线时备份的说明'],
      [/无法约束/, '明确说明 CommandWebUI 不受本项约束'],
      [/appBase\(\)/, '网关相对路径'],
    ];
    const lost = guards.filter(([re]) => !re.test(js)).map(([, n]) => n);
    if (lost.length === 0) ok('既有安全约束全部保留（确认分级/快照保护/无认证提示/相对路径）');
    else bad('有安全约束在重构中丢失：' + lost.join('、'));
    if (!/start_after/.test(js)) ok('三步不自动串联（无 start_after 残留）');
    else bad('仍有 start_after 自动串联残留');
  } catch (e) {
    bad('响应式/安全断言执行失败：' + e.message);
  }

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

  // 2h) Steam 扫码/进度界面的"卡住"路径（真机反馈：扫了码界面不动）
  //
  // 背景：用户反馈"手机上已经扫码登录了，但管理器上没刷新"。
  // 这里用真实 app.js + 可控的 fetch 把三条路径跑一遍：
  //   * 已保存登录（后端不会打印二维码）→ 界面不能说"正在生成二维码"；
  //   * 扫码成功 → 必须给出"已确认 / 正在下载"的反馈并显示进度；
  //   * 任务结束 → 必须给出明确的完成或失败提示。
  async function steamCase(name, { cached, savedUsername, frames, taskStates }) {
    const callsS = [];
    let frameIdx = 0;
    let stateIdx = 0;
    const fetchS = async (url) => {
      const u = String(url);
      callsS.push(u);
      const j = (o) => ({ ok: true, status: 200, headers: { get: () => 'application/json' },
                          json: async () => o, text: async () => JSON.stringify(o) });
      if (u.includes('native/steam/progress')) {
        const f = frames[Math.min(frameIdx, frames.length - 1)];
        frameIdx++;
        return j({ progress: f });
      }
      if (u.includes('native/steam/qr')) return j({ qr: null });
      if (u.includes('native/steam')) {
        return j({ installed: true, cached_login: !!cached, saved_username: savedUsername || '',
                   version: '3.4.0' });
      }
      if (u.includes('tasks/')) {
        const st = taskStates[Math.min(stateIdx, taskStates.length - 1)];
        stateIdx++;
        return j(st);
      }
      if (u.includes('config')) return j({ policy: {} });
      return j({});
    };
    const warned = [];
    const consoleS = Object.assign({}, console, { warn: (...a) => warned.push(a.map(String).join(' ')) });
    const sandboxS = Object.assign({}, sandbox, { fetch: fetchS, console: consoleS });
    Object.defineProperty(sandboxS, '__warned', { get: () => warned });
    // 让轮询快起来：把 3s 间隔换成 20ms
    sandboxS.setInterval = (fn) => setInterval(fn, 20);
    sandboxS.clearInterval = clearInterval;
    sandboxS.window = Object.assign({}, windowStub, { fetch: fetchS, setInterval: sandboxS.setInterval });
    sandboxS.__holder = makeEl('div');
    sandboxS.__hint = makeEl('div');
    sandboxS.__box = makeEl('div');
    sandboxS.globalThis = sandboxS;
    let ctxS;
    try {
      ctxS = vm.createContext(sandboxS);
      vm.runInContext(src, ctxS, { filename: 'app.js', timeout: 20000 });
    } catch (e) {
      bad(name + '：脚本求值异常 ' + e.message);
      return null;
    }
    const waitMs = 260;
    try {
      vm.runInContext(
        `pollSteamTask('t-steam', __holder, __hint, __box, { expectQr: ${cached ? 'false' : 'true'} });`,
        ctxS);
    } catch (e) {
      bad(name + '：pollSteamTask 抛异常 ' + e.message);
      return null;
    }
    await new Promise((r) => setTimeout(r, waitMs));
    let err = '';
    try { vm.runInContext('if (_qrTimer) clearInterval(_qrTimer);', ctxS); } catch (e) { /* ignore */ }
    return { hint: sandboxS.__hint.textContent, box: sandboxS.__box, calls: callsS,
             err, logged: sandboxS.__warned || [] };
  }

  // 场景一：已保存登录 —— 后端不会打印二维码
  {
    const r = await steamCase('免扫码场景', {
      cached: true, savedUsername: 'fee11one1y',
      frames: [{ stage: '启动 DepotDownloader', started_at: Math.floor(Date.now() / 1000) - 3 },
               { stage: '下载游戏文件', percent: 37.5, last_bytes: 171000000,
                 started_at: Math.floor(Date.now() / 1000) - 60 }],
      taskStates: [{ state: 'running', progress: 20, step: 'Steam 扫码登录并下载' }],
    });
    if (!r) { /* 已报错 */ } else {
      if (/生成二维码/.test(r.hint)) {
        bad('免扫码场景仍在提示"正在生成二维码" —— 用户会一直等一张不会出现的码（界面卡住）');
      } else ok('免扫码场景不再提示"生成二维码"（实际："' + r.hint.slice(0, 40) + '"）');
      if (/下载|登录|下载器/.test(r.hint)) ok('免扫码场景显示了进行中的阶段文案');
      else bad('免扫码场景没有给出任何进行中反馈：' + JSON.stringify(r.hint));
      if (r.calls.some((c) => c.includes('native/steam/progress'))) {
        ok('免扫码场景轮询了下载进度接口');
      } else {
        bad('免扫码场景没有轮询下载进度接口 —— 界面会显得卡住；'
            + '实际请求：' + JSON.stringify(r.calls.map((c) => String(c).replace(BASE, '/')))
            + ' 警告：' + JSON.stringify(r.logged));
      }
      const boxText = (r.box.children || []).map((c) => c.textContent || '').join(' ');
      if (/37\.5%|已下载/.test(boxText)) ok('进度框显示了下载进度：' + boxText.slice(0, 50));
      else bad('进度框没有显示下载进度：' + JSON.stringify(boxText));
    }
  }

  // 场景二：扫码成功 → 进入下载（不能停在"等待扫码"）
  {
    const r = await steamCase('扫码后进入下载', {
      cached: false, savedUsername: '',
      frames: [{ stage: '等待扫码', started_at: Math.floor(Date.now() / 1000) - 5 },
               { stage: '下载游戏文件', percent: 62.0, total_bytes: 458056832,
                 started_at: Math.floor(Date.now() / 1000) - 30 }],
      taskStates: [{ state: 'running', progress: 20, step: 'Steam 扫码登录并下载' }],
    });
    if (r) {
      if (/等待扫码|生成二维码/.test(r.hint) && /下载/.test(r.hint) === false) {
        bad('扫码成功进入下载后，界面仍停在等待扫码：' + JSON.stringify(r.hint));
      } else ok('扫码确认后界面切到进行中文案：' + JSON.stringify(r.hint.slice(0, 40)));
    }
  }

  // 场景三：任务完成 → 必须明确提示完成
  {
    const r = await steamCase('任务完成提示', {
      cached: true, savedUsername: 'fee11one1y',
      frames: [{ stage: '下载完成', total_bytes: 458056832,
                 started_at: Math.floor(Date.now() / 1000) - 120 }],
      taskStates: [{ state: 'succeeded', progress: 100, step: '已完成' }],
    });
    if (r) {
      if (/完成/.test(r.hint)) ok('任务完成后有明确提示：' + JSON.stringify(r.hint.slice(0, 30)));
      else bad('任务完成后界面没有提示完成：' + JSON.stringify(r.hint));
    }
  }

  // 场景四：任务失败 → 必须显示原因（不能只"卡住"）
  {
    const r = await steamCase('任务失败提示', {
      cached: false, savedUsername: '',
      frames: [{ stage: '等待扫码', started_at: Math.floor(Date.now() / 1000) - 200 }],
      taskStates: [{ state: 'failed', progress: 20, step: '失败',
                     error: 'DepotDownloader 退出码 1' }],
    });
    if (r) {
      if (/结束|失败/.test(r.hint) && /退出码/.test(r.hint)) {
        ok('任务失败时界面显示了真实原因');
      } else bad('任务失败时界面没有显示原因：' + JSON.stringify(r.hint));
    }
  }

  // 2i) 页面重复内容检查（用户指出：配置页与系统页有一模一样的按钮组）
  {
    const ast = fs.readFileSync(APP_JS, 'utf8');
    const starts = [...ast.matchAll(/^PAGES\.(\w+)\s*=\s*async/gm)]
      .map((m) => ({ name: m[1], at: m.index }));
    const ranges = starts.map((s, i) => [s.name, s.at,
      i + 1 < starts.length ? starts[i + 1].at : ast.length]);
    const ACTION_KEY = ['安装', '上传', '获取', '更新', 'Steam', '导入', '导出', '删除',
                        '重启', '启动', '停止', '回滚', '恢复', '诊断', '创建'];
    const pageActions = {};
    for (const [name, s, e] of ranges) {
      const chunk = ast.slice(s, e);
      const labels = [...chunk.matchAll(/text:\s*'([^']{2,42})'/g)]
        .map((m) => m[1])
        .filter((l) => ACTION_KEY.some((k) => l.includes(k)));
      pageActions[name] = new Set(labels);
    }
    // 找出跨页完全相同的"操作按钮名"
    const dupes = [];
    const names = Object.keys(pageActions);
    for (let i = 0; i < names.length; i++) {
      for (let j = i + 1; j < names.length; j++) {
        const a = pageActions[names[i]];
        const b = pageActions[names[j]];
        // 只比对"足够具体"的按钮名：像「恢复」「删除」这种通用动词在存档页与
        // 快照页各自作用于不同对象，是合理的，不该误报。
        const GENERIC = new Set(['恢复', '删除', '导出', '导入', '重启', '启动', '停止',
                                 '创建', '检查', '记录', '获取', '更新', '选择', '关闭']);
        const shared = [...a].filter((x) => b.has(x) && x.length >= 6 && !GENERIC.has(x)
          && !/说明|会|需要|必须|建议|将|则|时/.test(x));
        if (shared.length) dupes.push(names[i] + ' ↔ ' + names[j] + '：' + shared.join('、'));
      }
    }
    if (dupes.length === 0) ok('没有跨页面重复的操作按钮');
    else bad('存在跨页面重复的操作：' + dupes.join(' / '));

    // 组件/游戏本体操作只能出现在「系统」页
    const cfg = pageActions.config || new Set();
    const leaked = [...cfg].filter((l) =>
      /安装 \/ 检查显示组件|上传游戏本体|获取 \/ 更新 SMAPI|Steam 扫码登录|获取 \/ 更新游戏本体/.test(l));
    if (leaked.length === 0) ok('配置页不再重复「系统」页的组件/游戏本体操作');
    else bad('配置页仍重复系统页的操作按钮：' + leaked.join('、'));
    const sys = pageActions.system || new Set();
    if ([...sys].some((l) => l.includes('Steam 扫码登录'))) {
      ok('组件与游戏本体操作保留在「系统」页');
    } else bad('系统页缺少组件/游戏本体操作入口');

    // 「没有存档」时必须提示手动新建 + 重启（而不是自动建）
    const dash = ranges.find(([n]) => n === 'dashboard');
    if (dash) {
      const chunk = ast.slice(dash[1], dash[2]);
      if (/需手动新建存档并重启服务端/.test(chunk) && /openVnc/.test(chunk)
          && /重启服务器/.test(chunk)) {
        ok('「没有存档」时提示手动新建存档并重启服务端');
      } else bad('「没有存档」的提示不完整（应含手动新建步骤与重启入口）');
    }

    // 日志查看器只应有一个页面负责
    const logCards = ranges
      .filter(([name, s, e]) => /card\('日志'/.test(ast.slice(s, e)))
      .map(([name]) => name);
    if (logCards.length <= 1) ok('日志查看器没有跨页重复（当前：' + (logCards.join(',') || '无') + '）');
    else bad('多个页面都有「日志」卡片：' + logCards.join(', '));
  }

  if (server) { server.kill('SIGKILL'); }
  try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* ignore */ }
  report();
})();
