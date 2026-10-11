/* ValleyServer 管理器前端（无框架、无外部依赖，纯浏览器原生 JS）
 *
 * 重要：所有 API 调用都使用**相对路径**（"api/..."），
 * 这样在飞牛统一网关 /app/valleyserver/ 前缀下也能正确工作。
 */
'use strict';

const API_BASE = 'api/';
const PAGE_META = {
  dashboard: ['总览', '服务器状态、资源占用与快捷操作'],
  console: ['控制台', '实时日志、错误筛选与崩溃诊断'],
  saves: ['存档', '导入 / 导出 / 切换 / 恢复 / 删除'],
  mods: ['MOD', '安装、更新、启停、依赖与兼容性'],
  snapshots: ['快照', '创建、校验、恢复与保留策略'],
  versions: ['版本', '当前版本、更新检查、固定与回滚'],
  config: ['配置', '端口、启动策略、备份策略与运行参数'],
  tasks: ['任务', '队列、进度、历史与失败原因'],
  system: ['系统', '存储路径、依赖状态、日志与诊断包'],
  about: ['关于', '项目版本、上游版本、许可证与第三方声明'],
};

const ICONS = {
  dashboard: '▤', console: '▶', saves: '💾', mods: '🧩', snapshots: '⧉',
  versions: '🏷', config: '⚙', tasks: '☰', system: '🖥', about: 'ℹ',
};

const state = {
  csrf: '',
  route: 'dashboard',
  cfg: null,
  tasks: {},
  pollTimer: null,
  lastDashboard: null,
  showAllTasks: false,
  consoleFilter: 'all',
  consoleSource: 'container',
};

// ─────────────────────────────────────────────────────────────────────────────
// 基础工具
// ─────────────────────────────────────────────────────────────────────────────
const $ = (sel, root) => (root || document).querySelector(sel);
const el = (tag, attrs, children) => {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === 'class') node.className = v;
      else if (k === 'html') node.innerHTML = v;
      else if (k === 'text') node.textContent = v;
      else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
      else if (v === true) node.setAttribute(k, '');
      else node.setAttribute(k, String(v));
    }
  }
  for (const c of [].concat(children || [])) {
    if (c === null || c === undefined || c === false) continue;
    node.appendChild(typeof c === 'string' || typeof c === 'number'
      ? document.createTextNode(String(c)) : c);
  }
  return node;
};
const esc = (s) => String(s === null || s === undefined ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

function humanBytes(n) {
  n = Number(n || 0);
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + ' ' + units[i];
}
function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(Number(ts) * 1000);
  if (isNaN(d.getTime())) return String(ts);
  const p = (x) => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function fmtDuration(sec) {
  sec = Math.max(0, Math.floor(Number(sec) || 0));
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600);
  const m = Math.floor(sec % 3600 / 60), s = sec % 60;
  if (d) return `${d} 天 ${h} 小时`;
  if (h) return `${h} 小时 ${m} 分`;
  if (m) return `${m} 分 ${s} 秒`;
  return `${s} 秒`;
}
function fmtUptime(iso) {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (isNaN(t)) return '—';
  return fmtDuration((Date.now() - t) / 1000);
}

// ─────────────────────────────────────────────────────────────────────────────
// API
// ─────────────────────────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  const init = { method: opts.method || 'GET', headers: {}, credentials: 'same-origin' };
  if (opts.json !== undefined) {
    init.method = opts.method || 'POST';
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.json);
  }
  if (opts.body !== undefined) {
    init.method = opts.method || 'POST';
    init.body = opts.body;
  }
  if (state.csrf && init.method !== 'GET') init.headers['X-CSRF-Token'] = state.csrf;
  if (opts.headers) Object.assign(init.headers, opts.headers);

  let res;
  try {
    res = await fetch(API_BASE + path, init);
  } catch (err) {
    throw new Error('网络请求失败：' + err.message);
  }
  let data = null;
  const ctype = res.headers.get('content-type') || '';
  try {
    data = ctype.includes('application/json') ? await res.json() : await res.text();
  } catch (err) { data = null; }

  if (res.status === 401) {
    showGate('登录状态已失效，请重新登录。');
    throw new Error((data && data.error) || '未登录');
  }
  if (!res.ok) {
    const msg = (data && data.error) ? data.error : `HTTP ${res.status}`;
    const e = new Error(msg);
    e.status = res.status;
    e.data = data;
    throw e;
  }
  return data;
}

// ─────────────────────────────────────────────────────────────────────────────
// Toast / 模态框
// ─────────────────────────────────────────────────────────────────────────────
function toast(message, kind = 'info', ms = 4200) {
  const node = el('div', { class: 'toast ' + kind, text: message });
  $('#toast-root').appendChild(node);
  setTimeout(() => node.remove(), ms);
}

function modal({ title, body, wide, actions }) {
  const root = $('#modal-root');
  return new Promise((resolve) => {
    const close = (value) => { root.innerHTML = ''; resolve(value); };
    const foot = el('div', { class: 'modal-foot' });
    (actions || [{ label: '关闭', value: null, class: 'btn' }]).forEach((a) => {
      foot.appendChild(el('button', {
        class: a.class || 'btn', text: a.label,
        onclick: async () => {
          if (a.onClick) {
            const ok = await a.onClick(close);
            if (ok === false) return;
          }
          close(a.value);
        },
      }));
    });
    const box = el('div', { class: 'modal' + (wide ? ' wide' : '') }, [
      el('div', { class: 'modal-head', text: title }),
      el('div', { class: 'modal-body' }, [typeof body === 'string' ? el('div', { html: body }) : body]),
      foot,
    ]);
    root.innerHTML = '';
    root.appendChild(el('div', {
      class: 'modal-back', onclick: (e) => { if (e.target === e.currentTarget) close(null); },
    }, [box]));
  });
}

/** 高风险操作确认：必须让用户看到影响范围（而不是只有一个"确定"）。 */
async function confirmRisk({ title, action, impacts, danger = true, requireText = '' }) {
  const list = el('ul', {}, (impacts || []).map((t) => el('li', { text: t })));
  const body = el('div', {}, [
    danger ? el('div', {
      class: 'danger-box',
      html: '<strong>高风险操作</strong>：请先阅读下面的影响范围；系统会在执行前自动创建快照。',
    }) : null,
    el('div', { class: 'impact' }, [
      el('strong', { text: '操作：' }), el('span', { text: action }),
      el('div', { class: 'mt small muted', text: '影响范围：' }), list,
    ]),
  ]);
  let input = null;
  if (requireText) {
    input = el('input', { type: 'text', placeholder: requireText });
    body.appendChild(el('label', { class: 'field mt' }, [
      el('span', { class: 'lbl', text: `为避免误操作，请输入「${requireText}」以确认：` }),
      input,
    ]));
  }
  const ok = await modal({
    title: title || '请确认',
    body,
    actions: [
      { label: '取消', value: false, class: 'btn' },
      {
        label: '我确认，执行', value: true,
        class: danger ? 'btn-danger' : 'btn-primary',
        onClick: () => {
          if (requireText && input.value.trim() !== requireText) {
            toast(`请输入「${requireText}」以继续`, 'warn');
            return false;
          }
          return true;
        },
      },
    ],
  });
  return ok === true;
}

// ─────────────────────────────────────────────────────────────────────────────
// 任务跟踪
// ─────────────────────────────────────────────────────────────────────────────
async function submitTask(result, successMsg) {
  const taskId = result && result.task_id;
  if (!taskId) {
    toast(successMsg || '已提交', 'ok');
    return result;
  }
  toast((successMsg || '任务已提交') + `（任务 ${taskId}）`, 'info');
  pollTask(taskId);
  return result;
}

function taskBar(task) {
  const pct = Number(task.progress || 0);
  return el('div', {}, [
    el('div', { class: 'small muted mb', text: task.step || task.state }),
    el('div', { class: 'progress' }, [el('div', { style: `width:${pct}%` })]),
    el('div', { class: 'small muted mt', text: `进度 ${pct}%` }),
  ]);
}

async function pollTask(taskId) {
  state.tasks[taskId] = state.tasks[taskId] || { id: taskId };
  try {
    const data = await api(`tasks/${taskId}?logs=200`);
    state.tasks[taskId] = data;
    updateTaskChip(data);
    if (!data.terminal) {
      setTimeout(() => pollTask(taskId), 1500);
      return data;
    }
    if (data.state === 'succeeded') toast(`任务完成：${data.title}`, 'ok', 6000);
    else if (data.state === 'cancelled') toast(`任务已取消：${data.title}`, 'warn', 6000);
    else toast(`任务失败：${data.title} —— ${data.error || ''}`, 'err', 12000);
    refresh();
    return data;
  } catch (err) {
    updateTaskChip(null);
    return null;
  }
}

function updateTaskChip(task) {
  const chip = $('#task-chip');
  if (!task || task.terminal) { chip.hidden = true; return; }
  chip.hidden = false;
  $('#task-chip-text').textContent = `${task.title || task.kind_label} · ${task.progress || 0}%`;
  $('#task-chip-bar').style.width = (task.progress || 0) + '%';
}

// ─────────────────────────────────────────────────────────────────────────────
// 启动 / 登录门
// ─────────────────────────────────────────────────────────────────────────────
function showGate(message) {
  $('#app').hidden = true;
  $('#gate').hidden = false;
  const body = $('#gate-body');
  body.innerHTML = '';
  body.appendChild(el('p', { class: 'muted', text: message || '请输入管理口令以继续。' }));
  const input = el('input', { type: 'password', placeholder: '管理口令', autofocus: true });
  const btn = el('button', { class: 'btn btn-primary', text: '登录', style: 'width:100%;margin-top:12px' });
  const doLogin = async () => {
    btn.disabled = true;
    try {
      const res = await api('login', { json: { password: input.value } });
      state.csrf = res.csrf || '';
      await boot();
    } catch (err) {
      toast(err.message, 'err');
      input.select();
    } finally { btn.disabled = false; }
  };
  btn.addEventListener('click', doLogin);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') doLogin(); });
  body.appendChild(input);
  body.appendChild(btn);
  body.appendChild(el('p', {
    class: 'hint', style: 'margin-top:14px',
    text: '首次使用：口令在应用数据目录下 initial-password.txt 中（安装时生成）。',
  }));
  setTimeout(() => input.focus(), 50);
}

async function boot() {
  let auth;
  try { auth = await api('auth/state'); } catch (err) { showGate(); return; }
  if (!auth.authenticated) { showGate(); return; }
  $('#gate').hidden = true;
  $('#app').hidden = false;
  $('#btn-logout').hidden = auth.via_gateway;
  buildNav();
  if (!location.hash || !PAGE_META[location.hash.slice(1)]) {
    location.hash = '#' + state.route;
  } else {
    state.route = location.hash.slice(1);
  }
  await refresh();
  startGlobalPoll();
}

function buildNav() {
  const nav = $('#nav');
  nav.innerHTML = '';
  Object.entries(PAGE_META).forEach(([key, meta]) => {
    nav.appendChild(el('a', {
      href: '#' + key, 'data-route': key,
      onclick: () => { state.route = key; },
    }, [
      el('span', { class: 'ico', text: ICONS[key] || '•' }),
      el('span', { class: 'label', text: meta[0] }),
      el('span', { class: 'badge', id: 'badge-' + key, hidden: true }),
    ]));
  });
}

function markNav() {
  document.querySelectorAll('#nav a').forEach((a) => {
    a.classList.toggle('active', a.getAttribute('data-route') === state.route);
  });
  const meta = PAGE_META[state.route] || ['', ''];
  $('#page-title').textContent = meta[0];
  $('#page-desc').textContent = meta[1];
}

function setBadge(key, count) {
  const node = $('#badge-' + key);
  if (!node) return;
  if (count > 0) { node.hidden = false; node.textContent = count; }
  else node.hidden = true;
}

async function refresh() {
  state.route = (location.hash || '#dashboard').slice(1);
  if (!PAGE_META[state.route]) state.route = 'dashboard';
  markNav();
  const page = $('#page');
  page.innerHTML = '<p class="muted">加载中…</p>';
  try {
    await (PAGES[state.route] || PAGES.dashboard)(page);
  } catch (err) {
    console.error(err);
    page.innerHTML = '';
    page.appendChild(el('div', { class: 'banner err', text: '加载失败：' + err.message }));
    page.appendChild(el('div', { class: 'mt' }, [
      el('button', { class: 'btn', text: '重试', onclick: () => refresh() }),
    ]));
  }
}

function startGlobalPoll() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(async () => {
    try {
      const dash = await api('dashboard');
      state.lastDashboard = dash;
      renderNavState(dash);
      setBadge('tasks', (dash.tasks && dash.tasks.running && dash.tasks.running.length) || 0);
      const stale = (dash.snapshots && dash.snapshots.invalid) || 0;
      setBadge('snapshots', stale);
      setBadge('mods', (dash.mods && dash.mods.summary && dash.mods.summary.errors) || 0);
      const running = (dash.tasks && dash.tasks.running) || [];
      if (running.length) updateTaskChip({
        title: running[0].title || running[0].kind, progress: running[0].progress,
        terminal: false,
      });
      else updateTaskChip(null);
      if (!running.length) setBanner(dash);
    } catch (err) { /* 静默：轮询失败不打断用户 */ }
  }, 5000);
  api('dashboard').then((d) => { state.lastDashboard = d; renderNavState(d); setBanner(d); }).catch(() => {});
}

function renderNavState(dash) {
  const node = $('#nav-state');
  const phase = (dash.server && dash.server.phase) || '未知';
  const map = {
    '运行中': ['dot-ok', '运行中'], '启动中': ['dot-run', '启动中'],
    '已停止': ['dot-idle', '已停止'], '未安装': ['dot-idle', '未安装'],
    '启动失败': ['dot-err', '启动失败'], '未运行': ['dot-idle', '未运行'],
  };
  const [cls, text] = map[phase] || ['dot-warn', phase];
  node.innerHTML = '';
  node.appendChild(el('span', { class: 'dot ' + cls }));
  node.appendChild(el('span', { class: 'txt', text }));
}

function setBanner(dash) {
  const b = $('#banner');
  const messages = [];
  if (dash.security_warning) {
    messages.push({
      kind: 'warn',
      text: 'WebVNC/VNC/CommandWebUI 端口当前对所有网络接口开放。上游 WebVNC 无认证、'
          + 'VNC 使用镜像内固定密码、CommandWebUI 的 WebSocket 也无认证，建议改到「仅本机」。',
      action: { label: '去配置', route: 'config' },
    });
  }
  if (dash.disk && dash.disk.low) {
    messages.push({
      kind: 'err',
      text: `磁盘可用空间仅 ${humanBytes(dash.disk.free)}，低于策略下限 `
          + `${dash.disk.threshold_mb} MiB。系统将拒绝高风险变更。`,
      action: { label: '清理快照', route: 'snapshots' },
    });
  }
  if (dash.snapshots && dash.snapshots.count === 0) {
    messages.push({
      kind: 'info',
      text: '还没有任何快照。建议先创建一次手动快照，作为后续所有变更的安全基线。',
      action: { label: '去创建', route: 'snapshots' },
    });
  }
  if (dash.saves && dash.saves.invalid > 0) {
    messages.push({ kind: 'err', text: `检测到 ${dash.saves.invalid} 个结构不完整的存档，请到「存档」页查看。` });
  }
  if (!messages.length) { b.hidden = true; return; }
  b.hidden = false;
  b.className = 'banner ' + messages[0].kind;
  b.innerHTML = '';
  messages.forEach((m, i) => {
    const row = el('div', { style: i ? 'margin-top:6px' : '' }, [
      el('span', { text: m.text }),
    ]);
    if (m.action) {
      row.appendChild(el('button', {
        class: 'btn btn-sm', style: 'margin-left:10px', text: m.action.label,
        onclick: () => { location.hash = '#' + m.action.route; },
      }));
    }
    b.appendChild(row);
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// 通用渲染片段
// ─────────────────────────────────────────────────────────────────────────────
function card(title, children, extra) {
  return el('div', { class: 'card' }, [
    title ? el('div', { class: 'card-head' }, [
      el('h3', { text: title }), extra || null,
    ]) : null,
    ...[].concat(children || []).filter(Boolean),
  ]);
}
function metric(label, value, sub) {
  return el('div', { class: 'card metric' }, [
    el('span', { class: 'label', text: label }),
    el('span', { class: 'value', text: value }),
    sub ? el('span', { class: 'sub', text: sub }) : null,
  ]);
}
function tag(text, kind) { return el('span', { class: 'tag ' + (kind || 'idle'), text }); }
function phaseTag(phase) {
  const map = { '运行中': 'ok', '启动中': 'run', '已停止': 'idle', '未安装': 'idle', '启动失败': 'err' };
  return tag(phase || '未知', map[phase] || 'warn');
}
function kv(pairs) {
  const dl = el('dl', { class: 'kv' });
  pairs.forEach(([k, v]) => {
    dl.appendChild(el('dt', { text: k }));
    dl.appendChild(el('dd', {}, [typeof v === 'string' || typeof v === 'number'
      ? document.createTextNode(String(v)) : v]));
  });
  return dl;
}
function table(headers, rows) {
  return el('div', { class: 'table-wrap' }, [
    el('table', {}, [
      el('thead', {}, [el('tr', {}, headers.map((h) => el('th', { text: h })))]),
      el('tbody', {}, rows),
    ]),
  ]);
}
function taskResultBox(task) {
  const lines = [];
  lines.push(`状态：${task.state}（${task.state_label || ''}）`);
  lines.push(`类型：${task.kind_label || task.kind}　操作者：${task.operator}`);
  lines.push(`创建：${fmtTime(task.created_at)}`);
  if (task.started_at) lines.push(`开始：${fmtTime(task.started_at)}`);
  if (task.finished_at) lines.push(`结束：${fmtTime(task.finished_at)}　耗时：${fmtDuration(task.finished_at - task.started_at)}`);
  if (task.error) lines.push(`失败原因：${task.error}`);
  if (task.rollback_state) lines.push(`回滚：${task.rollback_state}${task.rollback_error ? ' —— ' + task.rollback_error : ''}`);
  const logs = (task.logs || []).map((l) => `[${l.level}] ${l.message}`).join('\n');
  return el('div', {}, [
    el('pre', { class: 'logbox', text: lines.join('\n') }),
    el('div', { class: 'small muted mt mb', text: '任务日志：' }),
    el('pre', { class: 'logbox', text: logs || '（无日志）' }),
  ]);
}

async function openTaskDetail(taskId) {
  const task = await api(`tasks/${taskId}`);
  await modal({
    title: `任务详情 · ${task.title}`,
    wide: true,
    body: taskResultBox(task),
    actions: [{ label: '关闭', value: null, class: 'btn' }],
  });
}

/** 文件上传：用原始请求体 + 元数据头，避免 multipart 解析复杂度。 */
function uploadBox({ accept, onUpload, label }) {
  const input = el('input', { type: 'file', accept: accept || '.zip', hidden: true });
  const drop = el('div', { class: 'drop', text: label || '点击选择文件，或把 ZIP 拖到这里' });
  const info = el('div', { class: 'fileinfo' });
  const pick = () => input.click();
  drop.addEventListener('click', pick);
  drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('hover'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('hover'));
  drop.addEventListener('drop', (e) => {
    e.preventDefault(); drop.classList.remove('hover');
    if (e.dataTransfer.files.length) handle(e.dataTransfer.files[0]);
  });
  input.addEventListener('change', () => { if (input.files.length) handle(input.files[0]); });

  async function handle(file) {
    info.textContent = `已选择：${file.name}（${humanBytes(file.size)}），正在上传…`;
    try {
      const res = await onUpload(file, info);
      info.textContent = (info.textContent || '') + ' 完成。';
      if (res) toast('上传完成', 'ok');
    } catch (err) {
      info.textContent = `上传失败：${err.message}`;
      toast(err.message, 'err');
    }
  }
  return el('div', {}, [drop, input, info]);
}

// ─────────────────────────────────────────────────────────────────────────────
// 页面：总览
// ─────────────────────────────────────────────────────────────────────────────
const PAGES = {};

PAGES.dashboard = async (root) => {
  const d = await api('dashboard');
  state.lastDashboard = d;
  const srv = d.server || {};
  const ready = srv.readiness || {};
  const st = srv.container || {};
  const stats = srv.stats || {};

  root.innerHTML = '';
  root.appendChild(el('div', { class: 'grid c4 mb' }, [
    metric('服务器状态', ready.phase || '未知', ready.detail || ''),
    metric('运行时长', st.running ? fmtUptime(st.started_at) : '—', st.running ? `容器 ${st.status}` : '容器未运行'),
    metric('CPU 占用', stats.available ? (stats.cpu_percent || '0') + ' %' : '不可用', stats.available ? `内存 ${stats.mem_usage || '—'}` : (stats.error || '')),
    metric('磁盘可用', d.disk ? humanBytes(d.disk.free) : '—',
      d.disk && d.disk.low ? '低于策略下限' : `下限 ${d.disk ? d.disk.threshold_mb : '?'} MiB`),
  ]));

  root.appendChild(el('div', { class: 'grid c2 mb' }, [
    card('快捷操作', [
      el('div', { class: 'btn-row' }, [
        el('button', { class: 'btn btn-ok', text: '启动服务器', onclick: () => serverAction('start') }),
        el('button', { class: 'btn', text: '重启服务器', onclick: () => serverAction('restart') }),
        el('button', { class: 'btn btn-danger', text: '停止服务器', onclick: () => serverAction('stop') }),
        el('button', { class: 'btn', text: '手动快照', onclick: () => createSnapshot('manual') }),
        el('button', { class: 'btn', text: '安装 / 首次启动', onclick: () => serverAction('install') }),
      ]),
      el('p', { class: 'hint mt', text: '高风险操作在执行前都会自动创建快照；快照失败则操作会被中止。' }),
      el('div', { class: 'mt' }, [
        el('div', { class: 'small muted mb', text: '访问入口（当前监听：' + (d.binding || '?') + '）' }),
        (() => {
          const port = (srv.ports && srv.ports.webvnc) || 5800;
          const host = d.binding === '127.0.0.1' ? '127.0.0.1' : location.hostname;
          const url = `http://${host}:${port}/vnc.html`;
          return el('div', { class: 'btn-row' }, [
            el('a', { class: 'btn btn-sm', href: url, target: '_blank', rel: 'noopener', text: '在新窗口打开 WebVNC' }),
            el('span', { class: 'small muted', text: url }),
          ]);
        })(),
      ]),
    ]),
    card('健康检查（三层判据）', [
      el('div', { class: 'mb' }, [
        tag('L1 容器运行', ready.layers && ready.layers.L1_container_running ? 'ok' : 'err'),
        ' ',
        tag('L2 端口就绪', ready.layers && ready.layers.L2_ports_listening ? 'ok' : 'idle'),
        ' ',
        tag('L3 SMAPI 已加载', ready.layers && ready.layers.L3_smapi_loaded ? 'ok' : 'idle'),
      ]),
      kv([
        ['容器名', st.exists ? 'valleyserver-game' : '（不存在）'],
        ['状态', st.status || '—'],
        ['健康', st.health && st.health !== 'none' ? st.health : '镜像无 Healthcheck（由管理器判定）'],
        ['重启次数', String(st.restart_count || 0)],
        ['镜像', st.image || '—'],
        ['镜像摘要', (st.image_digest || '').slice(0, 33) || '—'],
      ]),
      el('p', { class: 'hint mt', text: ready.detail || '' }),
    ]),
  ]));

  root.appendChild(el('div', { class: 'grid c2' }, [
    card('存档', [
      kv([
        ['数量', String((d.saves || {}).count || 0)],
        ['当前存档', (d.saves || {}).active || '（未指定）'],
        ['异常', String((d.saves || {}).invalid || 0)],
      ]),
      el('div', { class: 'btn-row mt' }, [
        el('button', { class: 'btn btn-sm', text: '打开存档管理', onclick: () => { location.hash = '#saves'; } }),
      ]),
    ]),
    card('MOD', [
      kv([
        ['已安装', String(((d.mods || {}).summary || {}).total || 0)],
        ['已启用', String(((d.mods || {}).summary || {}).enabled || 0)],
        ['错误', String(((d.mods || {}).summary || {}).errors || 0)],
        ['警告', String(((d.mods || {}).summary || {}).warnings || 0)],
      ]),
      el('div', { class: 'btn-row mt' }, [
        el('button', { class: 'btn btn-sm', text: '打开 MOD 管理', onclick: () => { location.hash = '#mods'; } }),
      ]),
    ]),
    card('快照', [
      kv([
        ['总数', String((d.snapshots || {}).count || 0)],
        ['自动', String((d.snapshots || {}).auto || 0)],
        ['手动', String((d.snapshots || {}).manual || 0)],
        ['未通过校验', String((d.snapshots || {}).invalid || 0)],
        ['最近一次', (d.snapshots || {}).latest
          ? `${(d.snapshots.latest).id} · ${fmtTime(d.snapshots.latest.created_at)}` : '（无）'],
      ]),
      el('div', { class: 'btn-row mt' }, [
        el('button', { class: 'btn btn-sm', text: '打开快照管理', onclick: () => { location.hash = '#snapshots'; } }),
      ]),
    ]),
    card('在线玩家', [
      el('div', { class: 'mb' }, [
        (d.players || {}).supported ? tag('已支持', 'ok') : tag('暂不支持', 'warn'),
      ]),
      el('p', { class: 'small muted', text: (d.players || {}).reason || '' }),
      el('p', { class: 'hint', text: '本页不展示推测数据：只有在上游接口能可靠提供时才会显示玩家列表。' }),
    ]),
  ]));
};

// ─────────────────────────────────────────────────────────────────────────────
// 服务器操作
// ─────────────────────────────────────────────────────────────────────────────
async function serverAction(kind) {
  const specs = {
    install: {
      path: 'server/install', title: '安装 / 首次启动',
      action: '拉取上游镜像并启动游戏服务器',
      impacts: [
        '从 Docker Hub 拉取 lixeer/valley-server（约 3.4 GiB），可能耗时较长。',
        '生成容器 valleyserver-game 与专属网络 valleyserver-net。',
        '不会删除或覆盖现有存档与 MOD。',
      ],
      danger: false,
    },
    start: {
      path: 'server/start', title: '启动服务器',
      action: '启动游戏容器并等待 SMAPI 加载完成',
      impacts: ['按当前配置启动容器。', '不会修改存档与 MOD。'],
      danger: false,
    },
    restart: {
      path: 'server/restart', title: '重启服务器',
      action: '重启游戏容器',
      impacts: ['在线玩家会被断开。', '执行前会创建快照（可取消）。'],
      danger: true,
    },
    stop: {
      path: 'server/stop', title: '停止服务器',
      action: '停止游戏容器',
      impacts: ['所有在线玩家会被断开。', '游戏进程停止后存档不再被写入。'],
      danger: true,
    },
    recreate: {
      path: 'server/recreate', title: '受控重建容器',
      action: '重新生成 compose 配置并重建容器',
      impacts: ['容器会被删除重建（镜像与数据卷不受影响）。', '执行前会创建快照。'],
      danger: true,
    },
  };
  const spec = specs[kind];
  if (!spec) return;
  const ok = await confirmRisk(spec);
  if (!ok) return;
  try {
    const body = kind === 'recreate' || kind === 'install' ? {} : { skip_snapshot: false };
    if (kind === 'restart') body.snapshot = true;
    const res = await api(spec.path, { json: body });
    await submitTask(res, spec.title + ' 已开始');
  } catch (err) {
    if (err.status === 409 && err.data && err.data.conflict) {
      const c = err.data.conflict;
      toast(`已有高风险任务在运行：${c.kind}（${c.id}），请等待其结束`, 'warn', 8000);
      location.hash = '#tasks';
    } else {
      toast(err.message, 'err');
    }
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// 页面：控制台
// ─────────────────────────────────────────────────────────────────────────────
PAGES.console = async (root) => {
  root.innerHTML = '';
  const [status, smapi] = await Promise.all([
    api('server/status'),
    api('mods/smapi-log').catch(() => ({ available: false })),
  ]);
  const ready = status.readiness || {};

  const tabs = el('div', { class: 'tabs' }, [
    ...[['container', '容器日志'], ['console', 'SMAPI 控制台'], ['manager', '管理器日志'], ['task', '任务日志']]
      .map(([key, label]) => el('button', {
        text: label, 'data-src': key,
        class: state.consoleSource === key ? 'active' : '',
        onclick: async () => { state.consoleSource = key; await renderLog(); markTabs(); },
      })),
  ]);
  function markTabs() {
    tabs.querySelectorAll('button').forEach((b) => {
      b.classList.toggle('active', b.getAttribute('data-src') === state.consoleSource);
    });
  }

  const logBox = el('pre', { class: 'logbox', text: '加载中…' });
  const filterRow = el('div', { class: 'btn-row mb' }, [
    ...['all', 'error', 'warn'].map((f) => el('button', {
      class: 'btn btn-sm' + (state.consoleFilter === f ? ' primary' : ''),
      text: f === 'all' ? '全部' : (f === 'error' ? '只看错误' : '只看警告'),
      onclick: () => { state.consoleFilter = f; renderLog(); },
    })),
    el('span', { style: 'flex:1' }),
    el('button', { class: 'btn btn-sm', text: '自动刷新', onclick: () => toggleAuto() }),
    el('button', { class: 'btn btn-sm', text: '下载日志', onclick: downloadLog }),
  ]);

  let auto = null;
  function toggleAuto() {
    if (auto) { clearInterval(auto); auto = null; toast('已停止自动刷新'); return; }
    auto = setInterval(renderLog, 4000);
    toast('已开启自动刷新（每 4 秒）');
  }

  async function renderLog() {
    try {
      const res = await api(`server/logs?source=${state.consoleSource}&tail=800&level=${
        state.consoleFilter === 'all' ? '' : state.consoleFilter}`);
      const lines = res.lines || [];
      logBox.innerHTML = '';
      if (!lines.length) {
        logBox.textContent = '（暂无日志）';
      } else {
        lines.forEach((line) => {
          let cls = 'l-info';
          if (/\b(error|critical|fatal|exception)\b/i.test(line)) cls = 'l-error';
          else if (/\bwarn(ing)?\b/i.test(line)) cls = 'l-warn';
          logBox.appendChild(el('div', { class: cls, text: line }));
        });
        logBox.scrollTop = logBox.scrollHeight;
      }
    } catch (err) { logBox.textContent = '读取失败：' + err.message; }
  }
  function downloadLog() {
    api(`server/logs?source=${state.consoleSource}&tail=5000`).then((res) => {
      const blob = new Blob([(res.lines || []).join('\n')], { type: 'text/plain' });
      const a = el('a', { href: URL.createObjectURL(blob),
        download: `valleyserver-${state.consoleSource}-${Date.now()}.log` });
      a.click();
    });
  }

  root.appendChild(el('div', { class: 'grid c3 mb' }, [
    metric('状态', ready.phase || '未知', ready.detail || ''),
    metric('VNC 画面', `${(status.ports || {}).webvnc || '?'} / ${(status.ports || {}).vnc || '?'}`, 'WebVNC / 原生 VNC'),
    metric('游戏端口', String((status.ports || {}).game || '?') + ' / UDP',
      `CommandWebUI ${(status.ports || {}).cmdwebui || '?'}`),
  ]));

  root.appendChild(card('崩溃诊断与 SMAPI 加载检查', [
    smapi.available
      ? el('div', {}, [
        el('div', { class: 'mb' }, [
          tag(`SMAPI ${(smapi.versions || {}).smapi || '未识别'}`, (smapi.versions || {}).smapi ? 'ok' : 'warn'), ' ',
          tag(`游戏 ${(smapi.versions || {}).game || '未识别'}`, (smapi.versions || {}).game ? 'ok' : 'warn'), ' ',
          tag(`已加载 MOD ${smapi.loaded_count || 0}`, (smapi.loaded_count ? 'ok' : 'warn')),
        ]),
        (smapi.errors || []).length
          ? el('div', {}, [
            el('div', { class: 'small muted mb', text: 'SMAPI 报错（确定性错误）：' }),
            ...(smapi.errors || []).map((e) => el('div', { class: 'issue error' }, [
              el('div', { class: 'issue-head' }, [
                tag(e.level === 'error' ? '错误' : '警告', e.level === 'error' ? 'err' : 'warn'),
                el('span', { text: e.mod || '（未关联到具体 MOD）' }),
              ]),
              el('div', { class: 'issue-body' }, [el('code', { text: e.line })]),
            ])),
          ])
          : el('p', { class: 'small', text: '未在日志中发现确定性错误。' }),
        (smapi.notes || []).length
          ? el('div', { class: 'hint mt', text: '待确认：' + smapi.notes.join('；') }) : null,
        el('p', { class: 'hint mt', text: smapi.certainty_policy || '' }),
      ])
      : el('p', { class: 'muted', text: '尚无 SMAPI 控制台输出（容器可能尚未启动，或仍处于冷启动阶段）。' }),
  ]));

  root.appendChild(card('日志', [tabs, filterRow, logBox]));
  await renderLog();
};

// ─────────────────────────────────────────────────────────────────────────────
// 页面：存档
// ─────────────────────────────────────────────────────────────────────────────
PAGES.saves = async (root) => {
  const data = await api('saves');
  root.innerHTML = '';

  root.appendChild(el('div', { class: 'grid c3 mb' }, [
    metric('存档数量', String(data.saves.length), `目录 ${data.saves_dir}`),
    metric('当前存档', data.active || '（未指定）', '下次启动游戏时加载'),
    metric('运行状态', data.idle ? '已停止（可安全操作）' : '运行中',
      data.idle ? '允许导入/删除/切换' : '写操作将被拒绝，需先停服'),
  ]));

  root.appendChild(el('div', { class: 'grid c2 mb' }, [
    card('导入存档', [
      uploadBox({
        label: '点击选择存档 ZIP（或拖入）；系统会先检查结构再导入',
        onUpload: async (file, info) => {
          const plan = await api('saves/import-plan', {
            body: file, headers: { 'X-Filename': file.name },
          });
          const cands = plan.plan.save_candidates || [];
          const chosen = plan.plan.plausible_candidate || (cands[0] || {}).name || '';
          info.textContent = `预检完成：${plan.plan.inspection.file_count} 个文件，`
            + `${humanBytes(plan.plan.inspection.total_uncompressed)}，识别到 ${cands.length} 个目录。`;
          if ((plan.plan.inspection.suspicious || []).length) {
            toast('压缩包存在可疑条目：' + plan.plan.inspection.suspicious.join('；'), 'warn', 9000);
          }
          const conflict = cands.find((c) => c.name === chosen && c.conflict);
          const ok = await confirmRisk({
            title: '确认导入存档',
            danger: !!conflict,
            action: `导入「${chosen}」到 Saves/`,
            impacts: conflict
              ? [`目标存档「${chosen}」已存在，将被覆盖（旧存档会先移入现场备份区）。`,
                 '导入前会自动创建快照。', '服务器必须处于停止状态。']
              : ['新增一个存档目录，不会改动其它存档。', '导入前会自动创建快照。',
                 '服务器必须处于停止状态。'],
            requireText: conflict ? chosen : '',
          });
          if (!ok) return null;
          const res = await api('saves/import', {
            body: file,
            headers: {
              'X-Filename': file.name,
              'X-Save-Name': chosen,
              'X-Overwrite': conflict ? 'true' : 'false',
            },
          });
          await submitTask(res, '存档导入');
          return res;
        },
      }),
    ]),
    card('说明', [
      el('ul', { class: 'list-tight small' }, [
        el('li', { text: '上传前会检查 ZIP 的路径、条目数、体积与压缩比，拒绝路径穿越、符号链接与疑似压缩炸弹。' }),
        el('li', { text: '导入前会检查目标是否已存在；覆盖必须二次确认（需手工输入存档名）。' }),
        el('li', { text: '游戏运行时会拒绝导入/删除/切换（游戏进程可能正在写盘）。' }),
        el('li', { text: '删除的存档不会直接抹掉，而是移入现场备份区（restore-tmp）。' }),
      ]),
    ]),
  ]));

  const rows = data.saves.map((s) => el('tr', {}, [
    el('td', {}, [
      el('div', { text: s.dir_name }),
      s.active ? el('div', {}, [tag('当前存档', 'ok')]) : null,
    ]),
    el('td', {}, [
      s.valid ? tag('结构完整', 'ok') : tag('结构异常', 'err'),
      s.problems && s.problems.length
        ? el('div', { class: 'small muted', text: s.problems.join('；') }) : null,
    ]),
    el('td', {}, [
      el('div', { text: s.farm_name || '—' }),
      s.player_name ? el('div', { class: 'small muted', text: '农场主：' + s.player_name }) : null,
    ]),
    el('td', { text: s.game_date || '—' }),
    el('td', { text: s.game_version || '—' }),
    el('td', { class: 'right', text: humanBytes(s.size_bytes) + ` / ${s.file_count} 文件` }),
    el('td', { text: fmtTime(s.modified_at) }),
    el('td', { class: 'actions' }, [
      el('button', { class: 'btn btn-sm', text: '导出', onclick: () => exportSave(s) }),
      el('button', {
        class: 'btn btn-sm', text: '切换为当前', disabled: s.active || !s.valid,
        onclick: () => activateSave(s),
      }),
      el('button', {
        class: 'btn btn-sm', text: '恢复', onclick: () => restoreSaveFromSnapshot(s),
      }),
      el('button', {
        class: 'btn btn-sm btn-danger', text: '删除',
        onclick: () => deleteSave(s),
      }),
    ]),
  ]));
  const tbl = data.saves.length
    ? table(['存档目录', '校验', '农场', '游戏日期', '游戏版本', '大小', '修改时间', '操作'], rows)
    : el('p', { class: 'muted', text: '暂无存档。请先导入，或在游戏中创建新存档。' });
  root.appendChild(card('存档列表', tbl));

  window.__saves = data;
};

async function exportSave(s) {
  try {
    const url = `${API_BASE}saves/export/download?name=${encodeURIComponent(s.dir_name)}`;
    const res = await fetch(url, { credentials: 'same-origin' });
    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try { const j = await res.json(); msg = j.error || msg; } catch (e) { /* ignore */ }
      throw new Error(msg);
    }
    const blob = await res.blob();
    const a = el('a', { href: URL.createObjectURL(blob), download: `${s.dir_name}.zip` });
    a.click();
    toast(`已导出 ${s.dir_name}`, 'ok');
  } catch (err) { toast(err.message, 'err'); }
}

async function activateSave(s) {
  const ok = await confirmRisk({
    title: '切换当前存档',
    danger: false,
    action: `把「${s.dir_name}」设为当前存档`,
    impacts: [
      '写入选择文件；下次启动游戏时会加载该存档。',
      '执行前会自动创建快照。',
      '如果选择「立即重启容器」，当前在线玩家会被断开。',
    ],
  });
  if (!ok) return;
  const restart = await modal({
    title: '是否立即重启容器？',
    body: el('p', { text: '立即重启会在数分钟内断开在线玩家并加载新存档。选择「稍后」则只记录选择，'
      + '你可以在方便时手动重启。' }),
    actions: [
      { label: '稍后再说', value: false, class: 'btn' },
      { label: '立即重启并加载', value: true, class: 'btn-primary' },
    ],
  });
  try {
    const res = await api('saves/activate', { json: { dir_name: s.dir_name, restart: restart === true } });
    await submitTask(res, '切换存档');
  } catch (err) { toast(err.message, 'err'); }
}

async function deleteSave(s) {
  const ok = await confirmRisk({
    title: '删除存档',
    action: `删除存档「${s.dir_name}」`,
    impacts: [
      '该存档目录会从 Saves/ 移除（移入现场备份区，可在需要时人工找回）。',
      '如果它是当前存档，选择会被清除。',
      '执行前会自动创建快照。',
      '服务器必须处于停止状态。',
    ],
    requireText: s.dir_name,
  });
  if (!ok) return;
  try {
    const res = await api('saves/delete', { json: { dir_name: s.dir_name, confirm: s.dir_name } });
    await submitTask(res, '删除存档');
  } catch (err) { toast(err.message, 'err'); }
}

async function restoreSaveFromSnapshot(s) {
  const snaps = await api('snapshots');
  const candidates = (snaps.snapshots || []).filter((x) => x.valid);
  if (!candidates.length) { toast('没有可用的有效快照', 'warn'); return; }
  const select = el('select', {}, candidates.map((x) => el('option', {
    value: x.id, text: `${x.id} · ${fmtTime(x.created_at)} · ${x.reason_label || x.reason}`,
  })));
  const ok = await modal({
    title: `从快照恢复存档「${s.dir_name}」`,
    body: el('div', {}, [
      el('div', { class: 'danger-box', text: '恢复会用快照中的内容替换当前存档目录，当前内容会被移入现场备份区。' }),
      el('label', { class: 'field' }, [el('span', { class: 'lbl', text: '选择快照' }), select]),
    ]),
    actions: [
      { label: '取消', value: false, class: 'btn' },
      { label: '恢复', value: true, class: 'btn-danger' },
    ],
  });
  if (ok !== true) return;
  try {
    const res = await api('saves/restore', {
      json: { snapshot_id: select.value, dir_name: s.dir_name },
    });
    await submitTask(res, '恢复存档');
  } catch (err) { toast(err.message, 'err'); }
}

// ─────────────────────────────────────────────────────────────────────────────
// 页面：MOD
// ─────────────────────────────────────────────────────────────────────────────
PAGES.mods = async (root) => {
  const data = await api('mods');
  root.innerHTML = '';
  const sum = data.summary || {};

  root.appendChild(el('div', { class: 'grid c4 mb' }, [
    metric('已安装', String(sum.total || 0), data.mods_dir),
    metric('已启用', String(sum.enabled || 0), `已禁用 ${sum.disabled || 0}`),
    metric('错误', String(sum.errors || 0), sum.errors ? '需要处理' : '无'),
    metric('警告', String(sum.warnings || 0), ''),
  ]));

  root.appendChild(el('div', { class: 'grid c2 mb' }, [
    card('上传安装 MOD', [
      uploadBox({
        label: '点击选择单个 MOD 的 ZIP（或拖入）；包内必须含 manifest.json',
        onUpload: async (file, info) => {
          const plan = await api('mods/install-plan', {
            body: file, headers: { 'X-Filename': file.name },
          });
          const m = plan.plan.mods[0] || {};
          info.textContent = `预检完成：${m.manifest ? m.manifest.name : '?'} v${m.manifest ? m.manifest.version : '?'}`;
          const conflict = m.dir_conflict || m.id_conflict;
          const ok = await confirmRisk({
            title: '确认安装 MOD',
            danger: !!conflict,
            action: `安装「${m.root}」`,
            impacts: conflict
              ? ['同名目录或相同 UniqueID 已存在，将被覆盖（旧目录先移入现场备份区）。',
                 'MOD 目录内可能含每存档运行数据（data/），会被一并替换。',
                 '执行前会自动创建快照。']
              : ['新增一个 MOD 目录。', '执行前会自动创建快照。', '服务器必须处于停止状态。'],
            requireText: conflict ? m.root : '',
          });
          if (!ok) return null;
          const res = await api('mods/install', {
            body: file,
            headers: {
              'X-Filename': file.name,
              'X-Mod-Dir': m.root || '',
              'X-Overwrite': conflict ? 'true' : 'false',
            },
          });
          await submitTask(res, '安装 MOD');
          return res;
        },
      }),
    ]),
    card('安装上游 MOD 包', [
      el('p', { class: 'small muted', text: '从上游 Release 下载官方维护的 5 个 MOD（Mods.zip）。'
        + '游戏本体与第三方 MOD 不在其中。' }),
      el('div', { class: 'btn-row mt' }, [
        el('button', { class: 'btn', text: '查看来源与校验信息', onclick: showBundleInfo }),
        el('button', { class: 'btn btn-primary', text: '下载并安装', onclick: downloadBundle }),
      ]),
      el('p', { class: 'hint mt', text: 'ALOS 基于 perkmi/Always-On-Server-for-Multiplayer（MIT + 附加协议）；'
        + '商业使用请署名原仓库地址。' }),
    ]),
  ]));

  if ((data.issues || []).length) {
    root.appendChild(card('诊断结果', (data.issues || []).map((i) => el('div', {
      class: 'issue ' + (i.level === 'error' ? 'error' : (i.level === 'warn' ? 'warn' : 'info')),
    }, [
      el('div', { class: 'issue-head' }, [
        tag(i.level === 'error' ? '错误' : (i.level === 'warn' ? '警告' : '提示'),
          i.level === 'error' ? 'err' : (i.level === 'warn' ? 'warn' : 'info')),
        el('span', { text: i.message }),
      ]),
      i.impact ? el('div', { class: 'issue-body', text: '影响：' + i.impact }) : null,
    ]))));
  }

  const rows = (data.mods || []).map((m) => el('tr', {}, [
    el('td', {}, [
      el('div', { text: m.manifest.name || m.dir_name }),
      el('div', { class: 'small muted', text: m.dir_name }),
    ]),
    el('td', {}, [
      m.enabled ? tag('已启用', 'ok') : tag('已禁用', 'warn'),
    ]),
    el('td', { text: m.manifest.version || '—' }),
    el('td', {}, [
      el('div', { class: 'mono small', text: m.manifest.unique_id || '（缺失）' }),
      m.manifest.problems && m.manifest.problems.length
        ? el('div', { class: 'small', style: 'color:#f2b3b3', text: m.manifest.problems.join('；') })
        : null,
    ]),
    el('td', { text: m.manifest.author || '—' }),
    el('td', {}, [
      el('div', { class: 'right', text: humanBytes(m.size_bytes) }),
      m.has_private_data ? el('div', {}, [tag(`含每存档数据 ${m.private_data_files}`, 'warn')]) : null,
    ]),
    el('td', {}, [
      m.manifest.dependencies && m.manifest.dependencies.length
        ? el('div', { class: 'small' }, m.manifest.dependencies.map((d) => el('div', {
          text: `${d.unique_id}${d.minimum_version ? ' ≥ ' + d.minimum_version : ''}`
            + (d.required === 'false' ? '（可选）' : ''),
        })))
        : el('span', { class: 'muted small', text: '无' }),
    ]),
    el('td', { class: 'actions' }, [
      el('button', {
        class: 'btn btn-sm', text: m.enabled ? '禁用' : '启用',
        onclick: () => toggleMod(m),
      }),
      el('button', {
        class: 'btn btn-sm btn-danger', text: '卸载',
        onclick: () => removeMod(m),
      }),
    ]),
  ]));
  root.appendChild(card('已安装 MOD', (data.mods || []).length
    ? table(['名称', '状态', '版本', 'UniqueID', '作者', '大小', '依赖', '操作'], rows)
    : el('p', { class: 'muted', text: 'MOD 目录为空。首次部署时这是正常的，但服务器不会无人值守运行；'
      + '请安装 ALOS 等自动化 MOD。' })));
};

async function showBundleInfo() {
  const info = await api('mods/upstream-bundle');
  await modal({
    title: '上游 MOD 包来源',
    body: kv([
      ['上游仓库', info.repo],
      ['Release 标签', info.tag],
      ['下载地址', info.url],
      ['说明', info.note],
      ['许可证', info.license_note],
    ]),
    actions: [{ label: '关闭', value: null, class: 'btn' }],
  });
}

async function downloadBundle() {
  const info = await api('mods/upstream-bundle');
  const ok = await confirmRisk({
    title: '下载并安装上游 MOD 包',
    action: `从 ${info.repo} 的 Release「${info.tag}」下载 Mods.zip 并安装`,
    impacts: [
      '会从 GitHub 下载约 86 KB 的 ZIP（含 5 个 MOD）。',
      '若本地已存在同名 MOD 目录，需要覆盖确认。',
      '执行前会自动创建快照；服务器必须处于停止状态。',
      '下载完成后界面会显示实际 SHA-256，供你与上游核对。',
    ],
    danger: false,
  });
  if (!ok) return;
  try {
    const res = await api('mods/upstream-bundle', { json: { url: info.url, overwrite: false } });
    toast(`下载完成，SHA-256：${(res.sha256 || '').slice(0, 32)}…`, 'info', 9000);
    await submitTask(res, '安装上游 MOD 包');
  } catch (err) { toast(err.message, 'err', 9000); }
}

async function toggleMod(m) {
  const enabling = !m.enabled;
  const ok = await confirmRisk({
    title: enabling ? '启用 MOD' : '禁用 MOD',
    action: `${enabling ? '启用' : '禁用'}「${m.manifest.name || m.dir_name}」`,
    impacts: [
      enabling ? '把 manifest.json.<suffix> 改回 manifest.json，SMAPI 会在下次启动时加载。'
               : '把 manifest.json 改名为 manifest.json.<suffix>，SMAPI 会跳过该 MOD。',
      '不会删除 MOD 目录或其中的每存档数据。',
      '执行前会自动创建快照。', '服务器必须处于停止状态。',
    ],
    danger: !enabling,
  });
  if (!ok) return;
  try {
    const res = await api('mods/toggle', { json: { dir_name: m.dir_name, enable: enabling } });
    await submitTask(res, enabling ? '启用 MOD' : '禁用 MOD');
  } catch (err) { toast(err.message, 'err'); }
}

async function removeMod(m) {
  const ok = await confirmRisk({
    title: '卸载 MOD',
    action: `卸载「${m.manifest.name || m.dir_name}」`,
    impacts: [
      'MOD 目录会移入现场备份区（不是直接删除）。',
      m.has_private_data
        ? `该 MOD 目录内含 ${m.private_data_files} 个每存档运行数据文件，会额外保留一份副本。`
        : '该 MOD 没有每存档数据。',
      '执行前会自动创建快照。', '服务器必须处于停止状态。',
    ],
    requireText: m.dir_name,
  });
  if (!ok) return;
  try {
    const res = await api('mods/remove', { json: { dir_name: m.dir_name, keep_private_data: true } });
    await submitTask(res, '卸载 MOD');
  } catch (err) { toast(err.message, 'err'); }
}

// ─────────────────────────────────────────────────────────────────────────────
// 页面：快照
// ─────────────────────────────────────────────────────────────────────────────
PAGES.snapshots = async (root) => {
  const data = await api('snapshots');
  root.innerHTML = '';
  const p = data.policy || {};

  root.appendChild(el('div', { class: 'grid c4 mb' }, [
    metric('快照总数', String((data.snapshots || []).length), data.dir),
    metric('磁盘可用', String(data.disk_free_mb || 0) + ' MiB', `策略下限 ${p.min_free_space_mb} MiB`),
    metric('自动保留', String(p.keep_auto) + ' 个', `每 ${p.interval_minutes} 分钟定时`),
    metric('手动保留', String(p.keep_manual) + ' 个', p.auto_enabled ? '自动快照已开启' : '自动快照已关闭'),
  ]));

  root.appendChild(el('div', { class: 'btn-row mb' }, [
    el('button', { class: 'btn btn-primary', text: '创建手动快照', onclick: () => createSnapshot('manual') }),
    el('button', { class: 'btn', text: '修改保留策略', onclick: () => editPolicy(p) }),
    el('button', { class: 'btn', text: '执行一次保留清理', onclick: () => enforceRetention() }),
  ]));

  const rows = (data.snapshots || []).map((s) => el('tr', {}, [
    el('td', {}, [
      el('div', { class: 'mono small', text: s.id }),
      s.pinned ? el('div', {}, [tag('已固定', 'info')]) : null,
    ]),
    el('td', {}, [s.valid ? tag('校验通过', 'ok') : tag('未通过/不完整', 'err')]),
    el('td', {}, [tag(s.kind === 'auto' ? '自动' : '手动', s.kind === 'auto' ? 'info' : 'ok'),
      el('div', { class: 'small muted', text: s.reason_label || s.reason })]),
    el('td', { text: fmtTime(s.created_at) }),
    el('td', { class: 'right', text: humanBytes(s.size_bytes) + ` / ${s.file_count} 文件` }),
    el('td', {}, [
      el('div', { class: 'small', text: `游戏 ${s.game_version || '未记录'}` }),
      el('div', { class: 'small muted', text: `SMAPI ${s.smapi_version || '未记录'}` }),
      el('div', { class: 'small muted mono', text: (s.image_digest || '').slice(0, 26) }),
    ]),
    el('td', { text: s.note || '—' }),
    el('td', { class: 'actions' }, [
      el('button', { class: 'btn btn-sm', text: '详情', onclick: () => showSnapshot(s.id) }),
      el('button', { class: 'btn btn-sm', text: '校验', onclick: () => verifySnapshot(s.id) }),
      el('button', { class: 'btn btn-sm', text: '恢复', onclick: () => restoreSnapshot(s) }),
      el('button', { class: 'btn btn-sm', text: '下载', onclick: () => downloadSnapshot(s.id) }),
      el('button', { class: 'btn btn-sm btn-danger', text: '删除', onclick: () => deleteSnapshot(s) }),
    ]),
  ]));
  root.appendChild(card('快照列表', (data.snapshots || []).length
    ? table(['快照 ID', '校验', '类型/原因', '时间', '体积', '版本档案', '备注', '操作'], rows)
    : el('p', { class: 'muted', text: '还没有快照。建议立即创建一个手动快照作为基线。' })));

  root.appendChild(card('一致性协议说明', [
    el('ol', { class: 'list-tight small' }, [
      el('li', { text: '检查服务器状态并锁定当前变更任务。' }),
      el('li', { text: '必要时安全停服，并确认游戏进程退出（绝不对运行中的存档做快照）。' }),
      el('li', { text: '复制数据到临时目录；生成文件清单与 SHA-256 校验值。' }),
      el('li', { text: '检查必要文件、大小、抽样校验与元信息。' }),
      el('li', { text: '全部通过后原子提交（同一文件系统 rename），并标记为有效。' }),
      el('li', { text: '任何一步失败都不会标记成功，也不会继续原定的高风险操作。' }),
    ]),
  ]));
};

async function createSnapshot(kind) {
  const note = await modal({
    title: '创建快照',
    body: el('div', {}, [
      el('label', { class: 'field' }, [
        el('span', { class: 'lbl', text: '备注（可选）' }),
        el('input', { type: 'text', id: 'snap-note', placeholder: '例如：升级 SVE 前' }),
      ]),
      el('p', { class: 'hint', text: '容器正在运行时会先安全停服，快照完成后再启动。' }),
    ]),
    actions: [
      { label: '取消', value: null, class: 'btn' },
      { label: '创建', value: 'go', class: 'btn-primary' },
    ],
  });
  if (note !== 'go') return;
  try {
    const res = await api('snapshots/create', {
      json: { kind: kind || 'manual', reason: kind === 'auto' ? 'schedule' : 'manual',
              note: ($('#snap-note') || {}).value || '' },
    });
    await submitTask(res, '创建快照');
  } catch (err) { toast(err.message, 'err'); }
}

async function showSnapshot(id) {
  const s = await api(`snapshots/${id}`);
  const m = s.manifest || {};
  await modal({
    title: `快照详情 · ${id}`,
    wide: true,
    body: el('div', {}, [
      kv([
        ['类型', s.kind === 'auto' ? '自动' : '手动'],
        ['原因', s.reason_label || s.reason],
        ['创建时间', fmtTime(s.created_at)],
        ['体积 / 文件数', humanBytes(s.size_bytes) + ` / ${s.file_count}`],
        ['manifest 版本', String(m.manifest_version)],
        ['schema', m.schema || '—'],
        ['游戏版本', s.game_version || '未记录'],
        ['SMAPI 版本', s.smapi_version || '未记录'],
        ['镜像引用', m.image_ref || s.image_ref || '—'],
        ['镜像摘要', s.image_digest || '—'],
        ['快照时是否停服', m.server_stopped_for_snapshot ? '是（保证一致性）' : '否'],
        ['操作者', m.operator || '—'],
        ['MOD 内容哈希', s.mods_hash || '—'],
        ['存档内容哈希', s.saves_hash || '—'],
        ['备注', s.note || '—'],
      ]),
      el('div', { class: 'mt' }, [
        el('div', { class: 'small muted mb', text: `文件清单（前 200 / 共 ${s.file_total}）：` }),
        el('pre', { class: 'logbox', text: (s.files_preview || [])
          .map((f) => `${f.sha256.slice(0, 12)}  ${String(f.size).padStart(9)}  ${f.path}`).join('\n') }),
      ]),
      (m.mods || []).length ? el('div', { class: 'mt' }, [
        el('div', { class: 'small muted mb', text: '快照记录的 MOD 清单：' }),
        el('pre', { class: 'logbox', text: JSON.stringify(m.mods, null, 2) }),
      ]) : null,
    ]),
    actions: [{ label: '关闭', value: null, class: 'btn' }],
  });
}

async function verifySnapshot(id) {
  try {
    const res = await api('snapshots/verify', { json: { snapshot_id: id, deep: true } });
    const task = await submitTask(res, '校验快照');
    if (task && task.task_id) {
      setTimeout(async () => {
        try {
          const t = await api(`tasks/${task.task_id}`);
          if (t.state === 'succeeded') {
            const v = (t.result || {}).verify || t.result || {};
            toast(`校验通过：检查 ${v.checked || 0} 个文件`, 'ok', 7000);
          }
        } catch (e) { /* ignore */ }
      }, 2500);
    }
  } catch (err) { toast(err.message, 'err'); }
}

async function restoreSnapshot(s) {
  const ok = await confirmRisk({
    title: '从快照恢复',
    action: `用快照 ${s.id} 覆盖当前的 Saves/ 与 Mods/`,
    impacts: [
      '当前存档与 MOD 会被快照中的内容替换。',
      '系统会先把**当前状态**再做一份快照（作为回滚点）。',
      '恢复前会做深度校验；校验不通过会直接拒绝并在界面说明原因。',
      '恢复失败会自动回滚，并保留故障现场。',
      '服务器必须处于停止状态。',
    ],
    requireText: s.id,
  });
  if (!ok) return;
  try {
    const res = await api('snapshots/restore', { json: { snapshot_id: s.id } });
    await submitTask(res, '从快照恢复');
  } catch (err) { toast(err.message, 'err'); }
}

async function deleteSnapshot(s) {
  const ok = await confirmRisk({
    title: '删除快照',
    action: `删除快照 ${s.id}`,
    impacts: [
      '该快照目录会被永久删除。',
      '如果它被固定，需要先取消固定（或在任务中使用强制删除）。',
      '被恢复任务引用的快照不会被删除。',
    ],
    requireText: s.id,
  });
  if (!ok) return;
  try {
    const res = await api('snapshots/delete', { json: { snapshot_id: s.id } });
    await submitTask(res, '删除快照');
  } catch (err) { toast(err.message, 'err'); }
}

function downloadSnapshot(id) {
  const a = el('a', { href: `${API_BASE}snapshots/download?id=${encodeURIComponent(id)}` });
  a.click();
}

async function enforceRetention() {
  const ok = await confirmRisk({
    title: '执行保留策略清理',
    action: '按当前保留策略删除超出的旧快照',
    impacts: [
      '自动快照与手动快照分别按各自的保留数量清理。',
      '已固定的快照与仍被任务引用的快照不会被删除。',
    ],
    danger: false,
  });
  if (!ok) return;
  try {
    const res = await api('snapshots/policy', { json: {} });
    await submitTask(res, '保留策略');
  } catch (err) { toast(err.message, 'err'); }
}

async function editPolicy(p) {
  const fields = {
    keep_auto_snapshots: ['自动快照保留数量', p.keep_auto],
    keep_manual_snapshots: ['手动快照保留数量', p.keep_manual],
    auto_snapshot_interval_minutes: ['自动快照间隔（分钟）', p.interval_minutes],
  };
  const inputs = {};
  const body = el('div', {});
  Object.entries(fields).forEach(([key, [label, value]]) => {
    inputs[key] = el('input', { type: 'number', value: String(value) });
    body.appendChild(el('label', { class: 'field' }, [
      el('span', { class: 'lbl', text: label }), inputs[key],
    ]));
  });
  const enabled = el('input', { type: 'checkbox' });
  enabled.checked = !!p.auto_enabled;
  body.appendChild(el('label', { class: 'field' }, [
    el('span', { class: 'lbl', text: '启用自动快照' }), enabled,
  ]));
  const ok = await modal({
    title: '快照保留策略',
    body,
    actions: [
      { label: '取消', value: false, class: 'btn' },
      { label: '保存', value: true, class: 'btn-primary' },
    ],
  });
  if (ok !== true) return;
  const payload = { auto_snapshot_enabled: enabled.checked };
  Object.entries(inputs).forEach(([k, i]) => {
    const v = parseInt(i.value, 10);
    if (!isNaN(v) && v >= 0) payload[k] = v;
  });
  try {
    const res = await api('snapshots/policy', { json: payload });
    await submitTask(res, '保存保留策略');
  } catch (err) { toast(err.message, 'err'); }
}

// ─────────────────────────────────────────────────────────────────────────────
// 页面：版本
// ─────────────────────────────────────────────────────────────────────────────
PAGES.versions = async (root) => {
  const data = await api('versions');
  root.innerHTML = '';
  const cur = data.current || {};
  const cfg = data.config || {};
  const cap = data.rollback_capability || {};

  root.appendChild(el('div', { class: 'grid c4 mb' }, [
    metric('游戏版本', cur.game_version || '未识别', '镜像内烘焙（不可单独升级）'),
    metric('SMAPI 版本', cur.smapi_version || '未识别', '随镜像一起变更'),
    metric('镜像标签', cfg.tag || '—', cfg.pin_digest ? '已按摘要固定' : '按标签拉取（不推荐）'),
    metric('回滚能力', cap.can_rollback ? '具备' : '不具备', cap.can_rollback ? '可回到已保留的镜像摘要' : (cap.missing || []).join('；')),
  ]));

  root.appendChild(el('div', { class: 'btn-row mb' }, [
    el('button', { class: 'btn btn-primary', text: '检查可用更新', onclick: checkUpdates }),
    el('button', { class: 'btn', text: '固定 / 取消固定当前版本', onclick: () => pinVersion(cfg) }),
    el('button', { class: 'btn', text: '手动回滚', onclick: () => manualRollback(cap) }),
  ]));

  root.appendChild(el('div', { class: 'grid c2 mb' }, [
    card('当前运行版本档案', [
      kv([
        ['镜像引用', cur.image_ref || '—'],
        ['镜像摘要', cur.image_digest || '—'],
        ['镜像创建时间', cur.image_created || '—'],
        ['游戏版本', cur.game_version || '未识别'],
        ['SMAPI 版本', cur.smapi_version || '未识别'],
        ['MOD 内容哈希', cur.mods_hash || '—'],
        ['已装 MOD 数', String(cur.mod_count || 0)],
        ['存档内容哈希', cur.saves_hash || '—'],
        ['存档数', String(cur.save_count || 0)],
        ['记录时间', fmtTime(cur.recorded_at)],
      ]),
    ]),
    card('能力边界（明确披露）', [
      el('ul', { class: 'list-tight small' }, (data.limitations || []).map((t) => el('li', { text: t }))),
      data.pinned ? el('div', { class: 'mt' }, [
        tag('当前已固定版本', 'ok'),
        el('div', { class: 'small muted mt', text: `${data.pinned.digest} · ${data.pinned.note || ''}` }),
      ]) : el('p', { class: 'hint mt', text: '当前没有固定的版本档案。' }),
    ]),
  ]));

  const hist = await api('versions/history?limit=30');
  const rows = (hist.history || []).map((h) => el('tr', {}, [
    el('td', { text: fmtTime(h.recorded_at) }),
    el('td', {}, [h.pinned ? tag('已固定', 'ok') : tag('历史', 'idle')]),
    el('td', { class: 'mono small', text: h.image_ref || '—' }),
    el('td', { class: 'mono small', text: (h.image_digest || '—').slice(0, 26) }),
    el('td', { text: h.game_version || '—' }),
    el('td', { text: h.smapi_version || '—' }),
    el('td', { text: h.note || '—' }),
    el('td', { class: 'actions' }, [
      el('button', {
        class: 'btn btn-sm', text: '回滚到该版本',
        onclick: () => rollbackTo(h),
      }),
    ]),
  ]));
  root.appendChild(card('版本档案（记录实际内容，不是 latest 标签）', (hist.history || []).length
    ? table(['记录时间', '固定', '镜像', '摘要', '游戏', 'SMAPI', '说明', '操作'], rows)
    : el('p', { class: 'muted', text: '暂无版本档案。安装或启动一次后会自动记录。' })));

  const upd = el('div');
  root.appendChild(card('更新检查结果', [upd,
    el('button', { class: 'btn btn-sm mt', text: '开始检查', onclick: checkUpdates }),
  ]));

  async function checkUpdates() {
    upd.innerHTML = '正在检查…';
    try {
      const r = await api('versions/check');
      upd.innerHTML = '';
      const img = r.image || {};
      upd.appendChild(el('div', { class: 'mb' }, [
        tag(img.update_available ? '发现更新' : '无更新',
          img.update_available ? 'warn' : 'ok'),
        img.error ? el('span', { class: 'small', style: 'color:#f2b3b3;margin-left:8px', text: img.error }) : null,
        img.note ? el('div', { class: 'hint', text: img.note }) : null,
      ]));
      upd.appendChild(kv([
        ['镜像仓库', img.repo || '—'],
        ['上游可用标签', String((img.available_tags || []).length)],
        ['当前标签', img.current_tag || '—'],
        ['当前摘要', (img.current_digest || '—').slice(0, 33)],
      ]));
      if ((img.available_tags || []).length) {
        upd.appendChild(el('div', { class: 'small muted mt mb', text: '上游标签列表（含摘要）：' }));
        upd.appendChild(el('pre', { class: 'logbox', text: (img.available_tags || [])
          .map((t) => `${t.tag.padEnd(14)} ${(t.digest || '').slice(0, 26)}  ${t.pushed || ''}`).join('\n') }));
      }
      const mb = r.mods_bundle || {};
      upd.appendChild(el('div', { class: 'mt' }, [
        el('div', { class: 'small muted mb', text: '上游 MOD 包：' }),
        kv([
          ['上游仓库', mb.repo || '—'],
          ['最新 Release', mb.latest_release || '—'],
          ['下载地址', mb.download_url || '—'],
          ['错误', mb.error || '无'],
        ]),
      ]));
      if (img.update_available) {
        upd.appendChild(el('div', { class: 'btn-row mt' }, [
          el('button', {
            class: 'btn btn-primary', text: '更新到该摘要',
            onclick: () => doUpdate({ target_digest: img.newer_digest }),
          }),
        ]));
      }
    } catch (err) { upd.innerHTML = ''; upd.appendChild(el('div', { class: 'banner err', text: err.message })); }
  }

  async function doUpdate(target) {
    const ok = await confirmRisk({
      title: '更新运行版本',
      action: `切换到 ${target.target_digest || target.target_tag}`,
      impacts: [
        '会先创建完整快照（含 Saves / Mods / 配置 / 版本档案）。',
        '会拉取目标镜像并重建容器。',
        '更新后会做容器健康检查、SMAPI 加载检查与存档可读性检查。',
        '若健康检查失败，会尝试自动回滚到旧镜像并恢复快照；失败时保留故障现场。',
      ],
      requireText: 'update',
    });
    if (!ok) return;
    try {
      const res = await api('versions/update', { json: target });
      await submitTask(res, '更新运行版本');
    } catch (err) { toast(err.message, 'err'); }
  }

  async function pinVersion(cfg2) {
    const isPinned = cfg2.pin_digest;
    const ok = await confirmRisk({
      title: isPinned ? '取消版本固定' : '固定当前版本',
      action: isPinned ? '改为按标签拉取（不推荐）'
                       : `固定到 ${cfg2.digest || '当前摘要'}`,
      impacts: isPinned
        ? ['取消后 compose 会按标签拉取，上游覆盖标签时内容可能变化。']
        : ['会把镜像摘要写入配置，之后一律按摘要拉取，保证内容不可变。',
           '同时会在版本档案中标记为「已固定」，保留策略不会清理它。'],
      danger: isPinned,
    });
    if (!ok) return;
    try {
      const res = await api('versions/pin', { json: { pin: !isPinned, digest: cfg2.digest } });
      await submitTask(res, '版本固定设置');
    } catch (err) { toast(err.message, 'err'); }
  }

  async function manualRollback(cap2) {
    if (!cap2.can_rollback) {
      await modal({
        title: '无法回滚',
        body: el('div', {}, [
          el('div', { class: 'danger-box', text: '当前不具备回滚条件。系统不会伪造「已回滚」的结果。' }),
          el('ul', { class: 'list-tight small' }, (cap2.missing || []).map((x) => el('li', { text: x }))),
          el('p', { class: 'small muted mt', text: (cap2.notes || []).join(' ') }),
        ]),
        actions: [{ label: '知道了', value: null, class: 'btn' }],
      });
      return;
    }
    await doUpdate({ target_digest: cap2.target_digest });
  }

  async function rollbackTo(h) {
    const ok = await confirmRisk({
      title: '回滚到该版本',
      action: `切换到 ${h.image_digest}`,
      impacts: [
        '会先创建快照；随后拉取（若本地没有）并运行该摘要对应的镜像。',
        '游戏版本会随之变化（游戏本体与 SMAPI 都在镜像里）。',
        '如果该镜像在本地与上游都不存在，回滚会失败并保留当前现场。',
      ],
      requireText: 'rollback',
    });
    if (!ok) return;
    try {
      const res = await api('versions/update', { json: { target_digest: h.image_digest } });
      await submitTask(res, '回滚版本');
    } catch (err) { toast(err.message, 'err'); }
  }
};

// ─────────────────────────────────────────────────────────────────────────────
// 页面：配置
// ─────────────────────────────────────────────────────────────────────────────
PAGES.config = async (root) => {
  const cfg = await api('config');
  root.innerHTML = '';
  const c = cfg.container || {};
  const p = cfg.policy || {};
  const meta = cfg._meta || {};

  if (cfg.docker && !cfg.docker.ready) {
    root.appendChild(el('div', { class: 'banner err mb' }, [
      el('strong', { text: 'Docker 依赖未满足：' }),
      el('span', { text: (cfg.docker.guidance || []).join(' ') }),
    ]));
  }

  const fields = {};
  function num(key, label, value, hint) {
    const input = el('input', { type: 'text', value: String(value) });
    fields['container.' + key] = { input, kind: 'int' };
    return el('label', { class: 'field' }, [
      el('span', { class: 'lbl', text: label }), input,
      hint ? el('div', { class: 'hint', text: hint }) : null,
    ]);
  }
  function str(key, label, value, hint) {
    const input = el('input', { type: 'text', value: String(value || '') });
    fields['container.' + key] = { input, kind: 'str' };
    return el('label', { class: 'field' }, [
      el('span', { class: 'lbl', text: label }), input,
      hint ? el('div', { class: 'hint', text: hint }) : null,
    ]);
  }
  function sel(key, label, value, options, hint) {
    const input = el('select', {}, options.map(([v, t]) => el('option', {
      value: v, text: t, selected: v === String(value) ? true : null,
    })));
    fields['container.' + key] = { input, kind: 'str' };
    return el('label', { class: 'field' }, [
      el('span', { class: 'lbl', text: label }), input,
      hint ? el('div', { class: 'hint', text: hint }) : null,
    ]);
  }
  function pol(key, label, value, hint) {
    const input = el('input', { type: 'text', value: String(value) });
    fields['policy.' + key] = { input, kind: 'int' };
    return el('label', { class: 'field' }, [
      el('span', { class: 'lbl', text: label }), input,
      hint ? el('div', { class: 'hint', text: hint }) : null,
    ]);
  }
  function bool(key, label, value, hint) {
    const input = el('input', { type: 'checkbox' });
    input.checked = !!value;
    fields['policy.' + key] = { input, kind: 'bool' };
    return el('label', { class: 'field' }, [
      el('span', { class: 'lbl', text: label }),
      el('div', {}, [input]),
      hint ? el('div', { class: 'hint', text: hint }) : null,
    ]);
  }

  root.appendChild(el('div', { class: 'grid c2 mb' }, [
    card('端口与访问', [
      num('game_port', '游戏联机端口（UDP）', c.game_port, '玩家通过该端口联机'),
      num('webvnc_port', 'WebVNC 端口', c.webvnc_port, '上游 WebVNC 无认证，建议保持仅本机'),
      num('vnc_port', 'VNC 端口', c.vnc_port, '使用管理器生成的随机密码'),
      num('cmdwebui_port', 'CommandWebUI 端口', c.cmdwebui_port, '该 MOD 的 WebSocket 无认证'),
      sel('host_bind', '监听范围', c.host_bind,
        [['127.0.0.1', '仅本机（推荐）'], ['0.0.0.0', '所有网络接口']],
        '游戏端口如需外网联机请选「所有网络接口」；WebVNC/VNC/CommandWebUI 会一并开放，请注意风险。'),
      el('label', { class: 'field' }, [
        el('span', { class: 'lbl', text: 'VNC 密码（恰好 8 位可见 ASCII）' }),
        (() => {
          const input = el('input', { type: 'text', value: c.vnc_password || '' });
          fields['container.vnc_password'] = { input, kind: 'str' };
          return input;
        })(),
      ]),
      el('p', { class: 'hint', text: meta.port_hints || '' }),
    ]),
    card('镜像与资源', [
      str('image', '镜像仓库', c.image, '默认 lixeer/valley-server'),
      str('image_tag', '镜像标签', c.image_tag, '仅在未按摘要固定时使用'),
      str('image_digest', '镜像摘要', c.image_digest, '形如 sha256:...；固定后按摘要拉取'),
      sel('pin_digest', '按摘要固定', c.pin_digest ? 'true' : 'false',
        [['true', '是（推荐，内容不可变）'], ['false', '否（按标签，可能变化）']]),
      str('shm_size', '共享内存', c.shm_size, '示例 512m'),
      str('cpu_limit', 'CPU 上限（留空不限制）', c.cpu_limit, '示例 1.5'),
      str('mem_limit', '内存上限（留空不限制）', c.mem_limit, '示例 4g'),
      sel('restart_policy', '重启策略', c.restart_policy,
        [['unless-stopped', 'unless-stopped（推荐）'], ['always', 'always'], ['on-failure', 'on-failure'], ['no', 'no']]),
      num('healthcheck_interval', '健康检查间隔（秒）', c.healthcheck_interval),
      num('healthcheck_retries', '健康检查重试次数', c.healthcheck_retries),
      num('healthcheck_start_period', '冷启动宽限期（秒）', c.healthcheck_start_period,
        '游戏冷启动较慢，建议 ≥180'),
    ]),
  ]));

  root.appendChild(el('div', { class: 'grid c2 mb' }, [
    card('备份与保留策略', [
      pol('keep_auto_snapshots', '自动快照保留数量', p.keep_auto_snapshots),
      pol('keep_manual_snapshots', '手动快照保留数量', p.keep_manual_snapshots),
      pol('auto_snapshot_interval_minutes', '自动快照间隔（分钟）', p.auto_snapshot_interval_minutes),
      pol('min_free_space_mb', '磁盘空间下限（MiB）', p.min_free_space_mb,
        '低于此值时拒绝执行高风险变更'),
      pol('snapshot_space_estimate_mb', '单次快照预留（MiB）', p.snapshot_space_estimate_mb),
    ]),
    card('运行与恢复策略', [
      bool('auto_snapshot_enabled', '启用自动快照', p.auto_snapshot_enabled),
      bool('supervisor_enabled', '启用后台监管（异常退出自动恢复）', p.supervisor_enabled),
      bool('auto_start_with_nas', '随 NAS 启动自动拉起服务器', p.auto_start_with_nas),
      pol('start_retry_max', '自动恢复最大重试次数', p.start_retry_max),
      pol('start_retry_backoff_seconds', '首次退避秒数（之后指数增长）', p.start_retry_backoff_seconds),
      pol('metrics_interval_seconds', '指标采样间隔（秒）', p.metrics_interval_seconds),
      bool('confirm_destructive', '破坏性操作要求二次确认', p.confirm_destructive),
    ]),
  ]));

  root.appendChild(el('div', { class: 'btn-row mb' }, [
    el('button', { class: 'btn btn-primary', text: '保存并应用（会重建容器）', onclick: save }),
    el('button', { class: 'btn', text: '放弃修改', onclick: () => refresh() }),
  ]));

  root.appendChild(card('数据目录', [
    kv([
      ['数据根目录', meta.data_root || '—'],
      ['应用家目录（升级保留）', meta.share_path || '—'],
      ['磁盘可用', (meta.disk_free_mb || 0) + ' MiB'],
      ['管理器面板端口', String(meta.panel_port || '')],
    ]),
    el('p', { class: 'hint mt', text: '存档、MOD、快照与版本档案都在应用家目录下，升级或卸载（默认）不会删除。' }),
  ]));

  async function save() {
    const changes = { container: {}, policy: {} };
    for (const [key, def] of Object.entries(fields)) {
      const [section, name] = key.split('.');
      const cur = section === 'container' ? c[name] : p[name];
      let value;
      if (def.kind === 'bool') value = def.input.checked;
      else if (def.kind === 'int') {
        value = parseInt(def.input.value, 10);
        if (isNaN(value)) { toast(`${name} 必须是整数`, 'err'); return; }
      } else value = def.input.value;
      if (JSON.stringify(value) !== JSON.stringify(cur)) changes[section][name] = value;
    }
    if (!Object.keys(changes.container).length && !Object.keys(changes.policy).length) {
      toast('没有检测到修改'); return;
    }
    const impactList = [];
    for (const [section, obj] of Object.entries(changes)) {
      for (const [k, v] of Object.entries(obj)) {
        const cur = section === 'container' ? c[k] : p[k];
        impactList.push(`${section}.${k}：${String(cur)} → ${String(v)}`);
      }
    }
    if (changes.container && Object.keys(changes.container).length) {
      impactList.push('端口或容器参数的变更会重建容器（在线玩家会断开）。');
    }
    const ok = await confirmRisk({
      title: '应用配置变更',
      action: '写入配置并重建容器',
      impacts: ['执行前会自动创建快照。', '配置会先经过严格校验；不合法或 compose 校验失败时会回滚配置文件。',
        ...impactList],
      danger: true,
    });
    if (!ok) return;
    try {
      const res = await api('config', { json: { changes } });
      await submitTask(res, '应用配置');
    } catch (err) { toast(err.message, 'err', 9000); }
  }
};

// ─────────────────────────────────────────────────────────────────────────────
// 页面：任务
// ─────────────────────────────────────────────────────────────────────────────
const STATE_LABEL = {
  pending: '待执行', running: '运行中', succeeded: '成功', failed: '失败',
  cancelled: '已取消', rolling_back: '正在回滚', rolled_back: '已回滚',
  rollback_failed: '回滚失败', interrupted: '已中断',
};
function stateTag(s) {
  const kind = { succeeded: 'ok', running: 'run', pending: 'idle', cancelled: 'warn',
    failed: 'err', rolling_back: 'warn', rolled_back: 'warn', rollback_failed: 'err',
    interrupted: 'err' }[s] || 'idle';
  return tag(STATE_LABEL[s] || s, kind);
}

PAGES.tasks = async (root) => {
  const data = await api(`tasks?limit=${state.showAllTasks ? 200 : 60}`);
  root.innerHTML = '';
  const stats = data.stats || {};
  const counts = stats.counts || {};

  root.appendChild(el('div', { class: 'grid c4 mb' }, [
    metric('运行中', String(counts.running || 0), `排队 ${stats.queued || 0}`),
    metric('成功', String(counts.succeeded || 0), ''),
    metric('失败', String(counts.failed || 0), counts.rollback_failed ? `回滚失败 ${counts.rollback_failed}` : ''),
    metric('已取消/中断', String((counts.cancelled || 0) + (counts.interrupted || 0)), ''),
  ]));

  if ((stats.running || []).length) {
    root.appendChild(card('正在执行', (stats.running || []).map((t) => el('div', { class: 'mb' }, [
      el('div', { class: 'mb' }, [
        stateTag(t.state), ' ',
        el('strong', { text: t.title || t.kind }),
        el('span', { class: 'small muted', text: `　${t.id}` }),
      ]),
      taskBar(t),
    ]))));
  }

  root.appendChild(el('div', { class: 'btn-row mb' }, [
    el('button', {
      class: 'btn', text: state.showAllTasks ? '只看最近 60 条' : '显示最近 200 条',
      onclick: () => { state.showAllTasks = !state.showAllTasks; refresh(); },
    }),
    el('button', { class: 'btn', text: '刷新', onclick: () => refresh() }),
  ]));

  const rows = (data.tasks || []).map((t) => el('tr', {}, [
    el('td', {}, [el('span', { class: 'mono small', text: t.id })]),
    el('td', {}, [stateTag(t.state)]),
    el('td', {}, [
      el('div', { text: t.title || t.kind_label || t.kind }),
      el('div', { class: 'small muted', text: t.kind_label || t.kind }),
    ]),
    el('td', { text: t.operator || '—' }),
    el('td', {}, [
      el('div', { class: 'small', text: fmtTime(t.created_at) }),
      t.finished_at ? el('div', { class: 'small muted', text: '耗时 ' + fmtDuration(t.finished_at - (t.started_at || t.created_at)) }) : null,
    ]),
    el('td', {}, [
      el('div', { class: 'progress', style: 'min-width:80px' }, [el('div', { style: `width:${t.progress || 0}%` })]),
      el('div', { class: 'small muted', text: (t.step || '') + ' ' + (t.progress || 0) + '%' }),
    ]),
    el('td', {}, [
      t.error ? el('div', { class: 'small', style: 'color:#f2b3b3', text: String(t.error).slice(0, 120) }) : null,
      t.rollback_state ? el('div', { class: 'small muted', text: '回滚：' + t.rollback_state }) : null,
    ]),
    el('td', { class: 'actions' }, [
      el('button', { class: 'btn btn-sm', text: '详情', onclick: () => openTaskDetail(t.id) }),
      t.cancellable && !t.terminal
        ? el('button', { class: 'btn btn-sm', text: '取消', onclick: () => cancelTask(t.id) }) : null,
    ]),
  ]));

  root.appendChild(card('任务队列与历史', (data.tasks || []).length
    ? table(['任务 ID', '状态', '操作', '操作者', '时间', '进度', '失败/回滚', '操作'], rows)
    : el('p', { class: 'muted', text: '暂无任务记录。' })));

  root.appendChild(card('状态机', [
    el('pre', { class: 'logbox', text:
      'pending ──▶ running ──▶ succeeded\n' +
      '                │\n' +
      '                ├──▶ cancelled        （用户取消，仅在允许取消的任务上）\n' +
      '                │\n' +
      '                └──▶ failed ──▶ rolling_back ──▶ rolled_back\n' +
      '                                      │\n' +
      '                                      └──▶ rollback_failed  （保留故障现场，需人工介入）\n\n' +
      '崩溃残留的 running / rolling_back 任务在管理器重启时会被标记为 interrupted，\n' +
      '绝不会被当成成功；同一服务器的高风险任务严格串行执行。' }),
  ]));
};

async function cancelTask(id) {
  const ok = await confirmRisk({
    title: '取消任务',
    action: `取消任务 ${id}`,
    impacts: ['任务会在下一个安全检查点停止。',
      '已经完成的部分（例如已创建的快照）不会被回滚。',
      '不保证可以立即中断外部命令。'],
    danger: true,
  });
  if (!ok) return;
  try {
    await api('tasks/cancel', { json: { task_id: id } });
    toast('已请求取消', 'warn');
    refresh();
  } catch (err) { toast(err.message, 'err'); }
}

// ─────────────────────────────────────────────────────────────────────────────
// 页面：系统
// ─────────────────────────────────────────────────────────────────────────────
PAGES.system = async (root) => {
  const [info, health] = await Promise.all([api('system'), api('system/health')]);
  root.innerHTML = '';
  const disk = info.disk || {};

  root.appendChild(el('div', { class: 'grid c4 mb' }, [
    metric('管理器版本', info.manager_version || '—', info.python || ''),
    metric('磁盘可用', disk.free_human || '—', `阈值 ${(info.env || {}).TRIM_APPNAME ? '' : ''}${health.counts ? '' : ''}`),
    metric('健康检查', health.status === 'ok' ? '正常' : (health.status === 'warn' ? '有警告' : '有错误'),
      `错误 ${(health.counts || {}).error || 0} · 警告 ${(health.counts || {}).warn || 0}`),
    metric('容器', (info.container || {}).exists ? ((info.container || {}).running ? '运行中' : '已停止') : '未安装',
      info.container && info.container.restart_count ? `重启 ${info.container.restart_count} 次` : ''),
  ]));

  root.appendChild(el('div', { class: 'btn-row mb' }, [
    el('button', { class: 'btn btn-primary', text: '生成诊断包', onclick: makeBundle }),
    el('button', { class: 'btn', text: '检查 Docker 依赖', onclick: () => refresh() }),
  ]));

  root.appendChild(el('div', { class: 'grid c2 mb' }, [
    card('路径', kv(Object.entries(info.paths || {}))),
    card('环境变量（关键项）', kv(Object.entries(info.env || {}))),
  ]));

  root.appendChild(el('div', { class: 'grid c2 mb' }, [
    card('存储占用', table(['类别', '路径', '占用'], Object.entries(info.storage_breakdown || {})
      .map(([k, v]) => el('tr', {}, [
        el('td', { text: k }), el('td', { class: 'mono small', text: v.path }),
        el('td', { class: 'right', text: v.human }),
      ])))),
    card('Docker 依赖', info.docker ? kv([
      ['Docker 可执行文件', info.docker.docker && info.docker.docker.found ? '已找到' : '未找到'],
      ['Docker 版本', (info.docker.docker || {}).version || '—'],
      ['Compose V2', info.docker.compose && info.docker.compose.found ? '可用' : '不可用'],
      ['Compose 版本', (info.docker.compose || {}).version || '—'],
      ['Docker Socket', info.docker.socket && info.docker.socket.exists ? '存在' : '不存在'],
      ['Socket 可访问', info.docker.socket && info.docker.socket.accessible ? '是' : '否'],
      ['总体可用', info.docker.ready ? '是' : '否'],
    ]) : el('p', { class: 'muted', text: '无数据' }),
      info.docker && (info.docker.guidance || []).length
        ? el('div', { class: 'banner warn mt' }, (info.docker.guidance || []).map((g) => el('div', { text: g })))
        : null),
  ]));

  const tabs = el('div', { class: 'tabs' }, [
    ...['manager', 'task', 'container'].map((src) => el('button', {
      text: src === 'manager' ? '管理器日志' : (src === 'task' ? '任务日志' : '容器日志'),
      onclick: async () => {
        tabs.querySelectorAll('button').forEach((b) => b.classList.remove('active'));
        const res = await api(`server/logs?source=${src}&tail=500`);
        logBox.textContent = (res.lines || []).join('\n') || '（暂无日志）';
      },
    })),
  ]);
  const logBox = el('pre', { class: 'logbox', text: '点击上方标签查看日志' });
  root.appendChild(card('日志', [tabs, logBox]));

  const audit = await api('system/audit?limit=100');
  root.appendChild(card('审计记录', (audit.audit || []).length
    ? table(['时间', '操作者', '动作', '目标', '结果'], (audit.audit || []).map((a) => el('tr', {}, [
      el('td', { text: fmtTime(a.ts) }), el('td', { text: a.actor }),
      el('td', { text: a.action }), el('td', { class: 'small', text: (a.target || '').slice(0, 80) }),
      el('td', {}, [a.ok ? tag('成功', 'ok') : tag('失败', 'err')]),
    ])))
    : el('p', { class: 'muted', text: '暂无审计记录。' })));

  root.appendChild(card('兼容性提示（规则推断，不代表实测结论）', [
    el('div', {}, (health.compat_rules || []).map((r) => el('div', { class: 'issue info' }, [
      el('div', { class: 'issue-head' }, [
        tag(r.level === 'certain' ? '确定性' : (r.level === 'inferred' ? '规则推断' : '无法确认'),
          r.level === 'certain' ? 'ok' : (r.level === 'inferred' ? 'warn' : 'idle')),
        el('span', { text: r.title }),
      ]),
      el('div', { class: 'issue-body', text: r.detail + '　检查方式：' + r.check }),
    ]))),
    el('p', { class: 'hint mt', text: health.disclaimer || '' }),
  ]));

  if ((health.findings || []).length) {
    root.appendChild(card('健康检查发现', (health.findings || []).map((f) => el('div', {
      class: 'issue ' + (f.level === 'error' ? 'error' : (f.level === 'warn' ? 'warn' : 'info')),
    }, [
      el('div', { class: 'issue-head' }, [
        tag(f.level === 'error' ? '错误' : (f.level === 'warn' ? '警告' : '提示'),
          f.level === 'error' ? 'err' : (f.level === 'warn' ? 'warn' : 'info')),
        tag(f.certainty === 'certain' ? '确定性' : (f.certainty === 'inferred' ? '规则推断' : '无法确认'), 'idle'),
        el('span', { text: f.message }),
      ]),
      f.detail ? el('div', { class: 'issue-body', text: f.detail }) : null,
    ]))));
  }
};

async function makeBundle() {
  try {
    const res = await api('diagnostics/bundle', { json: {} });
    const taskId = res.task_id;
    toast('正在生成诊断包…', 'info');
    const poll = async () => {
      const t = await api(`tasks/${taskId}`);
      if (!t.terminal) { setTimeout(poll, 1500); return; }
      if (t.state === 'succeeded' && t.result && t.result.name) {
        await modal({
          title: '诊断包已生成',
          body: el('div', {}, [
            el('p', { text: '诊断包已自动脱敏（口令、令牌、Authorization 头等已替换为 [REDACTED]），'
              + '内不含存档与 MOD 文件本体。披露前仍建议人工复核。' }),
            kv([['文件名', t.result.name], ['大小', humanBytes(t.result.size)]]),
          ]),
          actions: [
            { label: '关闭', value: null, class: 'btn' },
            {
              label: '下载', value: null, class: 'btn-primary',
              onClick: () => {
                el('a', { href: `${API_BASE}diagnostics/download?name=${encodeURIComponent(t.result.name)}` }).click();
              },
            },
          ],
        });
      } else {
        toast('诊断包生成失败：' + (t.error || ''), 'err', 9000);
      }
    };
    setTimeout(poll, 1200);
  } catch (err) { toast(err.message, 'err'); }
}

// ─────────────────────────────────────────────────────────────────────────────
// 页面：关于
// ─────────────────────────────────────────────────────────────────────────────
PAGES.about = async (root) => {
  const d = await api('about');
  root.innerHTML = '';
  root.appendChild(el('div', { class: 'grid c2 mb' }, [
    card('本管理器', kv([
      ['名称', d.manager.name],
      ['应用名（appname）', d.manager.appname],
      ['版本', d.manager.version],
      ['许可证', d.manager.license],
      ['源码', d.manager.source],
    ])),
    card('上游项目', kv([
      ['仓库', d.upstream.repo],
      ['地址', d.upstream.url],
      ['路线', d.upstream.mode],
      ['许可证', d.upstream.license],
      ['基线 commit', d.upstream.baseline_commit],
      ['基线标签', d.upstream.baseline_tag],
    ])),
  ]));

  root.appendChild(card('容器镜像（已核实的事实）', [
    kv([
      ['镜像引用', d.container_image.ref],
      ['镜像摘要', d.container_image.digest],
      ['说明', d.container_image.note],
      ['镜像内游戏版本', d.verified_facts.game_version_in_image],
      ['镜像内 SMAPI 版本', d.verified_facts.smapi_version_in_image],
      ['游戏本体是否烘焙进镜像', d.verified_facts.image_baked_content ? '是（启动时不下载）' : '否'],
      ['镜像是否自带 Healthcheck', d.verified_facts.image_has_healthcheck ? '是' : '否（由管理器自建）'],
    ]),
  ]));

  root.appendChild(card('第三方组件与许可证', table(
    ['组件', '许可证', '说明'],
    (d.third_party || []).map((t) => el('tr', {}, [
      el('td', { text: t.name }), el('td', { text: t.license }),
      el('td', { class: 'small', text: t.note || '' }),
    ])),
  )));

  root.appendChild(card('免责声明', el('ul', { class: 'list-tight small' },
    (d.disclaimer || []).map((t) => el('li', { text: t })))));
};

// ─────────────────────────────────────────────────────────────────────────────
// 启动
// ─────────────────────────────────────────────────────────────────────────────
window.addEventListener('hashchange', () => refresh());
document.addEventListener('DOMContentLoaded', async () => {
  $('#btn-refresh').addEventListener('click', () => refresh());
  $('#btn-newtab').addEventListener('click', () => {
    const url = location.pathname + location.search + '#'
      + ((location.hash || '#dashboard').slice(1));
    window.open(url, '_blank', 'noopener');
  });
  $('#btn-logout').addEventListener('click', async () => {
    try { await api('logout', { json: {} }); } catch (e) { /* ignore */ }
    location.reload();
  });
  await boot();
});
