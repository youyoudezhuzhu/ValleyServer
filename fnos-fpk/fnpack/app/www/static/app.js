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
  consoleSource: 'console',
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
// 是否要求二次确认（来自「运行与恢复策略」里的 confirm_destructive）。
// 说明：关闭后界面不再弹确认框；**服务端的快照与校验不受影响**。
// 需要"输入指定文字"才放行的少数操作（requireText）仍然强制确认 —— 那是防误触的最后一道闸。
async function confirmRequired() {
  if (state.confirmDestructive !== undefined) return state.confirmDestructive;
  try {
    const cfg = await api('config');
    state.confirmDestructive = ((cfg.policy || {}).confirm_destructive) !== false;
  } catch (err) {
    state.confirmDestructive = true;   // 取不到就按最安全处理
  }
  return state.confirmDestructive;
}

async function confirmRisk({ title, action, impacts, danger = true, requireText = '' }) {
  if (!requireText && !(await confirmRequired())) return true;
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
    // 标签页在后台时不轮询。
    // 实测：用户的 NAS 上开了两个标签页，每个每 5 秒拉一次 dashboard，
    // 而 dashboard 要扫 /proc 判断进程与端口占用，叠加起来是本可避免的常态开销。
    // 后台标签页没人看，没必要轮询；切回前台会立刻补一次（见下方 visibilitychange）。
    if (document.hidden) return;
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
  // 从后台切回前台时立刻刷新一次，避免看到过期状态
  if (!state._visibilityBound) {
    state._visibilityBound = true;
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) refresh();
    });
  }
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

// ─────────────────────────────────────────────────────────────────────────────
// 原生部署准备：显示组件 与 游戏本体
//
// 这两个函数曾被误删过一次（重写「关于」页时把到文件末尾的内容一起替换掉了），
// 结果是按钮点了没反应。tools/test-frontend.js 现在会逐页执行渲染函数，
// 引用不存在的函数会直接让测试失败。
// ─────────────────────────────────────────────────────────────────────────────
async function installXstack() {
  let info = null;
  try { info = await api('runtime'); } catch (err) { /* 拿不到就按未知处理 */ }
  const xst = (info && info.native && info.native.xstack) || {};
  const missing = (xst.missing_required || []).join('、') || '（无）';
  const ok = await confirmRisk({
    title: '安装虚拟显示组件',
    danger: false,
    action: '用 apt-get download + dpkg -x 把 Xvfb/openbox/x11vnc/websockify/noVNC 解包到应用数据目录',
    impacts: [
      '**不修改系统包状态**：只下载并解包到应用自己的数据目录，不安装到系统。',
      '约 19 MB 下载、解包后约 75 MB。',
      '卸载应用并选择「删除全部数据」时会一并清除。',
      '游戏本体与 SMAPI 是 .NET 6 自包含发布，因此**不需要安装 .NET 运行时**。',
      '当前缺少：' + missing,
    ],
  });
  if (!ok) return;
  toast('正在下载并安装显示组件，请稍候…', 'info', 10000);
  try {
    const res = await api('runtime/xstack', { json: { force: false } });
    if (res && res.ok) {
      toast('虚拟显示组件已就绪', 'ok');
      refresh();
      return;
    }
    await modal({
      title: '显示组件仍不完整',
      body: el('div', {}, [
        el('div', { class: 'banner err', text: '缺少：' + ((res && res.missing_required) || []).join('、') }),
        el('p', { class: 'mt', text: '可以先手动安装这些系统包，再把「配置」页的显示组件来源改为「系统已装」。' }),
        el('pre', { class: 'logbox', text: 'apt-get install -y xvfb x11vnc websockify openbox novnc' }),
      ]),
      actions: [{ label: '知道了', value: null, class: 'btn' }],
    });
  } catch (err) {
    toast(err.message, 'err', 12000);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Steam 扫码获取 / 更新游戏本体
//
// 为什么把二维码矩阵画成 SVG：DepotDownloader 在终端里用字符画打印二维码，
// 直接塞进 <pre> 会因字体宽高比失真而扫不出来。这里让后端把字符画**解析回
// 0/1 模块矩阵**（并用定位图案自校验），前端用 SVG 按整数倍缩放渲染，
// 矢量、无字体依赖，手机一定能扫。
// ─────────────────────────────────────────────────────────────────────────────
// 当前管理界面的目录前缀（去掉可能的文件名）。
// 直接 TCP 访问时是 "/"，经飞牛网关时是 "/app/valleyserver/"。
// 用它拼相对地址，界面在任何访问方式下都能正确跳转。
function appBase() {
  const p = location.pathname || '/';
  return p.endsWith('/') ? p : p.replace(/[^/]*$/, '');
}

function qrSvg(matrix, { size = 260, quiet = 4 } = {}) {
  const n = matrix.length;
  const total = n + quiet * 2;
  const rects = [];
  for (let r = 0; r < n; r += 1) {
    for (let c = 0; c < n; c += 1) {
      if (matrix[r][c]) {
        rects.push(`<rect x="${c + quiet}" y="${r + quiet}" width="1" height="1"/>`);
      }
    }
  }
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" `
    + `viewBox="0 0 ${total} ${total}" shape-rendering="crispEdges" role="img" `
    + `aria-label="Steam 登录二维码">`
    + `<rect width="${total}" height="${total}" fill="#ffffff"/>`
    + `<g fill="#000000">${rects.join('')}</g></svg>`;
  const wrap = el('div', { class: 'qr-box' });
  wrap.innerHTML = svg;
  return wrap;
}

async function steamFetch() {
  let st;
  try { st = await api('native/steam'); } catch (err) { toast(err.message, 'err', 10000); return; }

  const body = el('div', {});
  body.appendChild(el('div', { class: 'banner ok mb' }, [
    el('strong', { text: '用你自己的 Steam 账号扫码下载。' }),
    el('span', { text: '不会在 NAS 上保存你的 Steam 密码；扫码一次后会缓存登录密钥，'
      + '之后「更新游戏本体」无需再扫。' }),
  ]));
  body.appendChild(kv([
    ['Steam 工具', st.installed ? ('已就绪 ' + (st.version || '')) : '尚未准备'],
    ['工具许可证', (st.license || '') + '（运行时下载，不随本应用分发）'],
    ['Steam 应用', (st.app_name || '') + '（app ' + (st.app_id || '') + '）'],
    ['已缓存登录', st.cached_login ? '是（更新无需再扫码）' : '否（首次需要扫码）'],
  ]));

  const infoBox = el('p', { class: 'hint mt', text: '' });
  if (!st.installed) {
    body.appendChild(infoBox);
    body.appendChild(el('div', { class: 'btn-row mt' }, [
      el('button', {
        class: 'btn btn-primary', text: '准备 Steam 工具（约 33 MB）',
        onclick: async (ev) => {
          ev.target.disabled = true;
          try {
            const info = await api('native/steam/prepare', { json: { force: false } });
            toast('Steam 工具已就绪' + (info && info.version ? '（' + info.version + '）' : ''), 'ok');
            infoBox.textContent = '工具已就绪，请关闭本窗口后重新点「Steam 扫码获取游戏本体」。';
          } catch (err) { toast(err.message, 'err', 12000); }
          finally { ev.target.disabled = false; }
        },
      }),
    ]));
    await modal({ title: 'Steam 扫码获取游戏本体', wide: true, body,
                  actions: [{ label: '关闭', value: null, class: 'btn' }] });
    return;
  }

  const qrHolder = el('div', { class: 'mt' });
  body.appendChild(qrHolder);
  const hint = el('p', { class: 'hint', text: '点击「开始」后会出现二维码，'
    + '请用 Steam 手机 App（Steam Guard 页面）扫码确认。' });
  body.appendChild(hint);

  const startBox = el('div', { class: 'btn-row mt' }, [
    el('button', {
      class: 'btn btn-primary', text: '开始（生成二维码）',
      onclick: async (ev) => {
        ev.target.disabled = true;
        try {
          const res = await api('native/steam/fetch', { json: { start_after: true } });
          const taskId = res.task_id;
          hint.textContent = '正在生成二维码…（若 5 分钟内未扫码会刷新）';
          pollQr(taskId, qrHolder, hint);
          await submitTask(res, 'Steam 获取游戏本体');
        } catch (err) { toast(err.message, 'err', 12000); }
        finally { ev.target.disabled = false; }
      },
    }),
  ]);
  body.appendChild(startBox);

  await modal({ title: 'Steam 扫码获取游戏本体', wide: true, body,
                actions: [{ label: '关闭（任务继续在后台跑）', value: null, class: 'btn' }] });
}

// 二维码指纹：用来判断"这一张是不是新的"。
// ⚠️ 不能用模块数量判断 —— DepotDownloader 每次刷新的二维码都是 29×29，
//    长度永远相同，于是界面会**一直显示第一张（已过期）的码**。
//    实测后果：用户扫了界面上那张过期的码，手机端以为登录了、管理器这边却
//    认证失败（DepotDownloader 报 The QR code has changed / Failed to authenticate）。
function qrFingerprint(matrix) {
  let h = 0;
  for (let r = 0; r < matrix.length; r += 1) {
    for (let c = 0; c < matrix[r].length; c += 1) {
      h = ((h * 31) + (matrix[r][c] ? 1 : 7) + r * 3 + c) % 1000000007;
    }
  }
  return h;
}

let _qrTimer = null;
function pollQr(taskId, holder, hint) {
  if (_qrTimer) clearInterval(_qrTimer);
  let lastFp = null;
  let refreshes = 0;
  let loginDone = false;
  const stop = () => { if (_qrTimer) { clearInterval(_qrTimer); _qrTimer = null; } };

  _qrTimer = setInterval(async () => {
    try {
      const r = await api('native/steam/qr');
      if (r.qr && r.qr.matrix) {
        const fp = qrFingerprint(r.qr.matrix);
        if (fp !== lastFp) {
          lastFp = fp;
          refreshes += 1;
          holder.innerHTML = '';
          holder.appendChild(qrSvg(r.qr.matrix));
          holder.appendChild(el('p', { class: 'small muted', text:
            `二维码 ${r.qr.modules}×${r.qr.modules} 模块（QR 版本 ${r.qr.version}）`
            + (refreshes > 1 ? ` · 已刷新 ${refreshes} 次，请扫**最新这一张**` : '')
            + ' · 扫码后在手机上确认，NAS 会自动开始下载' }));
          if (!loginDone) {
            hint.textContent = refreshes > 1
              ? '二维码已刷新（旧码失效）—— 请扫最新这一张'
              : '等待扫码确认…';
          }
        }
      } else if (!loginDone && lastFp !== null) {
        // 后端把二维码状态清空了：要么登录成功，要么任务结束
        loginDone = true;
        holder.innerHTML = '';
        holder.appendChild(el('p', { class: 'small muted', text: '✅ 扫码已确认' }));
      }

      const t = await api('tasks/' + taskId);
      if (t) {
        const terminal = ['succeeded', 'failed', 'cancelled', 'rolled_back',
                          'rollback_failed'].includes(t.state);
        if (terminal) {
          stop();
          holder.innerHTML = '';
          hint.textContent = t.state === 'succeeded'
            ? '完成：游戏本体已就绪，可以启动服务器了'
            : ('结束（' + t.state + '）：' + (t.error || ''));
        } else if (loginDone) {
          // 已经扫过码：进入下载阶段，显示任务进度
          hint.textContent = '扫码已确认，正在下载游戏文件…'
            + (t.progress ? `（${t.progress}%）` : '');
        }
      }
    } catch (err) { /* 轮询失败继续试 */ }
  }, 3000);
}

async function installSmapi() {
  let info = null;
  try { info = await api('native/smapi/releases'); } catch (err) { toast(err.message, 'err', 10000); return; }
  const game = info.game_version || '（未识别）';
  const rec = info.recommended || '';
  const installed = (info.installed && info.installed.version) || '';
  const sel = el('select', {}, (info.releases || []).map((r) => el('option', {
    value: r.version,
    text: `${r.version}${r.prerelease ? '（预览版）' : ''}　${r.compatible_note}`,
    selected: r.version === rec ? true : null,
  })));
  const forceBox = el('input', { type: 'checkbox' });
  const body = el('div', {}, [
    el('p', { class: 'small muted', text: 'SMAPI 从官方 Release 获取（' + (info.repo || 'Pathoschild/SMAPI')
      + '），由管理器整合进游戏目录。' }),
    kv([
      ['检测到的游戏版本', game],
      ['推荐 SMAPI 版本', rec || '（无兼容版本，见下）'],
      ['当前已整合', installed || '无'],
    ]),
    (info.releases || []).length
      ? el('label', { class: 'field' }, [
          el('span', { class: 'lbl', text: '要安装的 SMAPI 版本' }), sel,
          el('div', { class: 'hint', text: '列表按发布时间排序；标注为兼容要求的来自 SMAPI 官方发布说明。' }),
        ])
      : el('div', { class: 'banner err mt', text: '没有取到任何 SMAPI 版本，请检查 NAS 能否访问 api.github.com。' }),
    el('label', { class: 'field' }, [
      el('span', { class: 'lbl', text: '强制重新获取（即使已安装同版本）' }), el('div', {}, [forceBox]),
    ]),
  ]);
  const ok = await modal({
    title: '获取 / 更新 SMAPI',
    wide: true,
    body,
    actions: [
      { label: '取消', value: null, class: 'btn' },
      { label: '开始', value: 'go', class: 'btn btn-primary' },
    ],
  });
  if (ok !== 'go') return;
  try {
    const res = await api('native/smapi', { json: { version: sel.value, force: forceBox.checked } });
    await submitTask(res, '获取 SMAPI');
  } catch (err) { toast(err.message, 'err', 12000); }
}

async function uploadGameFiles() {
  let st = null;
  try { st = await api('native/gamefiles'); } catch (err) { /* 忽略 */ }
  st = st || {};
  const body = el('div', {}, [
    el('div', { class: 'banner warn mb' }, [
      el('strong', { text: '合规说明：' }),
      el('span', { text: '本项目不打包、不再分发《星露谷物语》游戏本体。'
        + '请上传你自己已合法拥有的游戏目录。' }),
    ]),
    el('div', { class: 'banner ok mb' }, [
      el('strong', { text: '只需要上传游戏本体。' }),
      el('span', { text: 'SMAPI 由管理器自己从官方 Release 获取并整合，'
        + '按游戏版本自动选兼容版本（无需你自己装）。' }),
    ]),
    kv([
      ['目标目录', (st.game_root) || '（未知）'],
      ['游戏本体', (st.smapi && st.smapi.game_version) ? ('已放入（游戏 ' + st.smapi.game_version + '）')
                                                       : (st.launcher_present ? '已放入' : '尚未放入')],
      ['当前体积', st.size_human || '0 B'],
      ['SMAPI', (st.smapi && st.smapi.installed) ? ('已整合 ' + (st.smapi.version || '')) : '尚未整合'],
      ['MOD 目录已链接', st.mods_linked ? '是' : '否'],
    ]),
    (st.smapi && !st.smapi.installed) ? el('div', { class: 'btn-row mt' }, [
      el('button', { class: 'btn', text: '现在就获取 SMAPI', onclick: installSmapi }),
    ]) : null,
    el('div', { class: 'mt' }, [
      uploadBox({
        label: '点击选择游戏本体 ZIP（或拖入）',
        accept: '.zip',
        onUpload: async (file, info) => {
          info.textContent = '正在上传 ' + file.name + '（' + humanBytes(file.size) + '）…';
          const res = await api('native/gamefiles', {
            body: file, headers: { 'X-Filename': file.name },
          });
          await submitTask(res, '安装游戏本体');
          return res;
        },
      }),
    ]),
    el('div', { class: 'mt' }, [
      el('div', { class: 'small muted mb', text: '包内必须包含：' }),
    ].concat((st.requirements || []).map((r) => el('div', { class: 'hint', text: '· ' + r })))),
    (st.warnings || []).length
      ? el('div', { class: 'banner err mt' },
          (st.warnings || []).map((w) => el('div', { class: 'small', text: w })))
      : null,
    (st.sources || []).length
      ? el('div', { class: 'mt' }, [
          el('div', { class: 'small muted mb', text: '可以从哪里取得游戏本体：' }),
        ].concat((st.sources || []).map((s) => el('div', { class: 'mt' }, [
          el('div', { class: 'small', text: '· ' + s.name }),
          el('div', { class: 'hint', style: 'margin-left:14px', text: s.detail || '' }),
          s.url ? el('div', { style: 'margin-left:14px' }, [
            el('a', { class: 'small', href: s.url, target: '_blank', rel: 'noopener', text: s.url }),
          ]) : null,
          s.ref ? el('div', { class: 'hint mono', style: 'margin-left:14px',
                              text: s.ref + (s.digest ? '  ' + s.digest : '') }) : null,
        ]))))
      : null,
  ]);
  await modal({
    title: '上传游戏本体与 SMAPI（原生部署）',
    wide: true,
    body,
    actions: [{ label: '关闭', value: null, class: 'btn' }],
  });
}

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
    metric('运行时长', st.running ? fmtUptime(st.started_at) : '—', st.running ? '游戏进程存活' : '游戏未运行'),
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
          // 用**相对地址**经管理器自己反代，而不是拼 host:port。
          // 原先写的是 `http://127.0.0.1:5800/vnc.html`：按推荐把 host_bind 设为
          // 127.0.0.1 后，这个链接只有在这台 NAS 本机上才打得开，
          // 从 fnconnect / 局域网其它设备访问必然失败（那是用户自己电脑的 127.0.0.1）。
          // 反代后"能打开管理界面的地方就能看画面"，也不需要额外暴露 5800 端口。
          const url = appBase() + 'vnc/vnc.html?autoconnect=1&resize=scale'
            + '&path=' + encodeURIComponent(appBase() + 'vnc/websockify');
          return el('div', { class: 'btn-row' }, [
            el('a', { class: 'btn btn-sm', href: url, target: '_blank', rel: 'noopener',
              text: '在新窗口打开 WebVNC' }),
            el('span', { class: 'small muted', text: '经管理器反代，远程（含 fnconnect）可用' }),
          ]);
        })(),
      ]),
    ]),
    card('健康检查（三层判据）', [
      el('div', { class: 'mb' }, [
        tag('L1 游戏进程', ready.layers && ready.layers.L1_game_process ? 'ok' : 'err'),
        ' ',
        tag('L2 端口就绪', ready.layers && ready.layers.L2_ports_listening ? 'ok' : 'idle'),
        ' ',
        tag('L3 SMAPI 已加载', ready.layers && ready.layers.L3_smapi_loaded ? 'ok' : 'idle'),
      ]),
      kv([
        ['启动器', st.exists ? (st.image || 'StardewModdingAPI') : '（不存在）'],
        ['状态', st.status || '—'],
        ['健康', st.health && st.health !== 'none' ? st.health : '镜像无 Healthcheck（由管理器判定）'],
        ['重启次数', String(st.restart_count || 0)],
        ['镜像', st.image || '—'],
        ['游戏本体目录', st.image || '—'],
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
        '启动虚拟显示栈（Xvfb / openbox / x11vnc / noVNC），再拉起游戏进程。',
        '不会删除或覆盖现有存档与 MOD。',
        '需要已安装显示组件并放入游戏本体（总览页会给出准备入口）。',
      ],
      danger: false,
    },
    start: {
      path: 'server/start', title: '启动服务器',
      action: '启动显示栈与游戏，并等待 SMAPI 加载完成',
      impacts: ['按当前配置启动。', '不会修改存档与 MOD。'],
      danger: false,
    },
    restart: {
      path: 'server/restart', title: '重启服务器',
      action: '重启显示栈与游戏',
      impacts: ['在线玩家会被断开。', '执行前会创建快照（可取消）。'],
      danger: true,
    },
    stop: {
      path: 'server/stop', title: '停止服务器',
      action: '停止游戏与显示栈',
      impacts: ['所有在线玩家会被断开。', '游戏进程停止后存档不再被写入。'],
      danger: true,
    },
    recreate: {
      path: 'server/recreate', title: '受控重建运行环境',
      action: '彻底停止显示栈与游戏后重新拉起',
      impacts: ['游戏进程与显示栈会被停止并重新启动。', '执行前会创建快照。'],
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
    ...[['console', 'SMAPI 控制台'], ['manager', '管理器日志'], ['task', '任务日志'], ['x11vnc', 'x11vnc']]
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
      : el('p', { class: 'muted', text: '尚无 SMAPI 控制台输出（游戏可能尚未启动，或仍处于冷启动阶段）。' }),
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
      '如果选择「立即重启」，当前在线玩家会被断开。',
    ],
  });
  if (!ok) return;
  const restart = await modal({
    title: '是否立即重启游戏？',
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

// 取上游 MOD 包信息，并**校验返回结构**。
// 曾经这里没校验：后端路由被通配路由遮蔽、返回的是 MOD 对象时，
// info.url 是 undefined，于是把一个 undefined 提交回后端，最后报出
// "只允许从上游仓库下载 MOD 包"这句与真实原因无关的提示（见 docs/known-issues.md I-1）。
async function bundleInfo() {
  const info = await api('mods/upstream-bundle');
  if (!info || typeof info.url !== 'string' || !info.url) {
    throw new Error('接口返回的不是上游 MOD 包信息（缺少下载地址）。'
      + '这通常是后端路由异常，请查看管理器日志。');
  }
  return info;
}

async function showBundleInfo() {
  let info;
  try { info = await bundleInfo(); } catch (err) { toast(err.message, 'err', 12000); return; }
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
  let info;
  try { info = await bundleInfo(); } catch (err) { toast(err.message, 'err', 12000); return; }
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
      el('div', { class: 'small muted mono', text: (s.mods_hash || '无 MOD 指纹').slice(0, 26) }),
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
      el('p', { class: 'hint', text: '游戏正在运行时会先安全停服，快照完成后再启动。' }),
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
        ['MOD 指纹', (s.mods_hash || '—').slice(0, 16)],
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
  const cap = data.rollback_capability || {};
  const files = data.game_files || {};
  const hist = data.history || [];

  const limitations = data.limitations || cap.notes || [];

  root.appendChild(el('div', { class: 'grid c4 mb' }, [
    metric('游戏版本', cur.game_version || '未识别', '从控制台日志或游戏本体读取'),
    metric('SMAPI 版本', cur.smapi_version || '未识别', '随游戏本体一起替换'),
    metric('MOD 指纹', (cur.mods_hash || '—').slice(0, 10) || '—',
      `${cur.mod_count || 0} 个 MOD`),
    metric('存档指纹', (cur.saves_hash || '—').slice(0, 10) || '—',
      `${cur.save_count || 0} 个存档`),
  ]));

  root.appendChild(el('div', { class: 'banner warn mb' }, [
    el('strong', { text: '原生部署的版本能力边界：' }),
    el('ul', { class: 'list-tight small', style: 'margin:6px 0 0' },
      limitations.map((t) => el('li', { text: t }))),
  ]));

  root.appendChild(el('div', { class: 'btn-row mb' }, [
    el('button', { class: 'btn btn-primary', text: '检查可用更新', onclick: checkUpdates }),
    el('button', {
      class: 'btn', text: '记录当前版本指纹', onclick: recordNow,
    }),
    el('button', {
      class: 'btn', text: cap.can_rollback ? '从最新快照回滚' : '创建快照后可回滚',
      disabled: !cap.can_rollback,
      onclick: () => rollbackFromSnapshot(),
    }),
  ]));

  root.appendChild(el('div', { class: 'grid c2 mb' }, [
    card('游戏本体', [
      kv([
        ['目录', files.root || '—'],
        ['启动器', files.launcher_present ? '已放入' : '未放入'],
        ['资源目录 Content/', files.content_present ? '存在' : '缺失'],
        ['文件数', String(files.file_count || 0)],
        ['体积', humanBytes(files.size_bytes || 0)],
        ['MOD 目录已链接', files.mods_linked ? '是' : '否'],
      ]),
      el('p', { class: 'hint mt', text: '原生部署下「更新」= 替换游戏本体文件；'
        + '新版文件需要你自行获取（本项目不再分发游戏本体）。' }),
    ]),
    card('回滚能力（基于快照）', [
      el('div', { class: 'mb' }, [
        cap.can_rollback ? tag('具备', 'ok') : tag('不具备', 'err'),
        el('span', { class: 'small muted', text: `  方式：${cap.method === 'snapshot' ? '快照恢复' : '（无）'}` }),
      ]),
      kv([
        ['可用快照数', String(cap.snapshot_count || 0)],
        ['最新快照', (cap.latest_snapshot && cap.latest_snapshot.id) || '—'],
      ]),
      (cap.missing || []).length
        ? el('div', { class: 'banner err mt' },
            (cap.missing || []).map((m) => el('div', { class: 'small', text: m })))
        : el('p', { class: 'hint mt', text: '回滚会把游戏本体、MOD、存档与配置一起恢复到所选快照的时点。' }),
    ]),
  ]));

  root.appendChild(card('版本档案（内容指纹）', [
    hist.length
      ? table(['时间', '游戏', 'SMAPI', 'MOD 指纹', '固定', '备注'], hist.map((h) => el('tr', {}, [
          el('td', { class: 'small', text: h.recorded_at_iso || '' }),
          el('td', { text: h.game_version || '—' }),
          el('td', { text: h.smapi_version || '—' }),
          el('td', { class: 'mono small', text: (h.mods_hash || '—').slice(0, 12) }),
          el('td', { text: h.pinned ? '已固定' : '—' }),
          el('td', { class: 'small', text: h.note || '' }),
        ])))
      : el('p', { class: 'muted', text: '尚无版本档案。安装或记录一次后会出现。' }),
  ]));

  let updates = null;
  async function checkUpdates() {
    try {
      updates = await api('versions/updates');
    } catch (err) { toast(err.message, 'err'); return; }
    const box = $('#versions-updates');
    box.innerHTML = '';
    const bundle = (updates && updates.mods_bundle) || {};
    box.appendChild(el('div', {}, [
      kv([
        ['上游 MOD 包来源', bundle.repo || '—'],
        ['最新发布', bundle.latest_release || '（未获取到）'],
        ['本地基线', (updates && updates.local_baseline) || '—'],
        ['是否有更新', bundle.update_available ? '可能有' : '未发现'],
      ]),
      bundle.error ? el('div', { class: 'banner warn mt', text: '检查失败：' + bundle.error }) : null,
      bundle.download_url
        ? el('div', { class: 'mt' }, [
            el('a', { class: 'btn', href: bundle.download_url, target: '_blank',
                      rel: 'noopener', text: '打开发布页' }),
          ])
        : null,
      el('p', { class: 'hint mt', text: '这里只检查上游发布的 MOD 包；'
        + '游戏本体更新需要你自己准备新版文件后上传。' }),
    ]));
  }

  async function recordNow() {
    const ok = await confirmRisk({
      title: '记录当前版本指纹',
      danger: false,
      action: '把当前的游戏版本、SMAPI 版本、MOD 与存档指纹写入版本档案',
      impacts: ['只读操作，不修改任何游戏文件。', '用于事后核对"这次和上次到底一样不一样"。'],
    });
    if (!ok) return;
    try {
      const res = await api('versions/record', { json: {} });
      await submitTask(res, '记录版本指纹');
    } catch (err) { toast(err.message, 'err', 9000); }
  }

  async function rollbackFromSnapshot() {
    const snap = cap.latest_snapshot || {};
    const ok = await confirmRisk({
      title: '从快照回滚',
      action: `恢复到快照 ${snap.id || ''}`,
      danger: true,
      requireText: 'rollback',
      impacts: [
        '游戏本体、MOD、存档与配置会被**替换为快照中的内容**。',
        '当前状态会先被移入 restore-tmp 保留现场（失败时用于回退）。',
        '游戏会先被安全停止，回滚完成后按需重新启动。',
      ],
    });
    if (!ok) return;
    try {
      const res = await api('versions/rollback', { json: { snapshot_id: snap.id || '' } });
      await submitTask(res, '从快照回滚');
    } catch (err) { toast(err.message, 'err', 9000); }
  }

  root.appendChild(card('检查上游 MOD 包更新', [
    el('div', { id: 'versions-updates' }, [
      el('p', { class: 'muted', text: '点击上方「检查可用更新」获取上游 MOD 包信息。' }),
    ]),
  ]));
};

PAGES.config = async (root) => {
  const cfg = await api('config');
  root.innerHTML = '';
  const n = cfg.native || {};
  const p = cfg.policy || {};
  const meta = cfg._meta || {};

  let rt = null;
  try { rt = await api('runtime'); } catch (err) { /* ignore */ }
  const xst = (rt && rt.native && rt.native.xstack) || {};

  const fields = {};

  function field(section, key, label, value, kind, hint, options) {
    let input;
    if (kind === 'bool') {
      input = el('input', { type: 'checkbox' });
      input.checked = !!value;
      fields[section + '.' + key] = { input, kind };
      return el('label', { class: 'field' }, [
        el('span', { class: 'lbl', text: label }), el('div', {}, [input]),
        hint ? el('div', { class: 'hint', text: hint }) : null,
      ]);
    }
    if (kind === 'select') {
      input = el('select', {}, (options || []).map(([v, t]) => el('option', {
        value: v, text: t, selected: String(v) === String(value) ? true : null,
      })));
    } else {
      input = el('input', { type: 'text', value: String(value === undefined || value === null ? '' : value) });
    }
    fields[section + '.' + key] = { input, kind: kind === 'int' ? 'int' : 'str' };
    return el('label', { class: 'field' }, [
      el('span', { class: 'lbl', text: label }), input,
      hint ? el('div', { class: 'hint', text: hint }) : null,
    ]);
  }
  const nat = (k, l, v, kind, hint, opts) => field('native', k, l, v, kind, hint, opts);
  const pol = (k, l, v, kind, hint) => field('policy', k, l, v, kind, hint);

  // ── 运行环境（原生部署）──
  root.appendChild(el('div', { class: 'grid c2 mb' }, [
    card('运行环境', [
      el('div', { class: 'mb' }, [
        tag('原生部署', 'ok'),
        rt && rt.detect && rt.detect.ready ? tag('依赖就绪', 'ok') : tag('依赖未就绪', 'err'),
      ]),
      el('p', { class: 'small muted', text: '管理器直接在本机运行 游戏客户端 + SMAPI + '
        + '自动化 MOD + 虚拟显示栈。游戏与 SMAPI 是 .NET 6 自包含发布，'
        + '不需要安装 .NET 运行时。' }),
      el('div', { class: 'btn-row mt' }, [
        el('button', { class: 'btn btn-primary', text: '安装 / 检查显示组件', onclick: installXstack }),
        el('button', { class: 'btn', text: '上传游戏本体', onclick: uploadGameFiles }),
        el('button', { class: 'btn', text: '获取 / 更新 SMAPI', onclick: installSmapi }),
    el('button', { class: 'btn btn-primary', text: 'Steam 扫码获取游戏本体', onclick: steamFetch }),
      ]),
      (rt && rt.detect && (rt.detect.guidance || []).length)
        ? el('div', { class: 'banner warn mt' },
            rt.detect.guidance.map((g) => el('div', { class: 'small', text: g })))
        : null,
      el('div', { class: 'mt' }, [
        el('div', { class: 'small muted mb', text: '显示组件状态（由管理器捆绑在应用目录内，不修改系统包状态）：' }),
        el('pre', { class: 'logbox', text: Object.entries(xst)
          .filter(([k, v]) => v && typeof v === 'object' && 'found' in v)
          .map(([k, v]) => (v.found ? '✔ ' : '✖ ') + k.padEnd(12) + ' ' + (v.source || '') + ' ' + (v.path || ''))
          .join('\n') || '（未检测）' }),
      ]),
      kv([
        ['游戏本体目录', (rt && rt.native && rt.native.game_root) || '—'],
        ['MOD 目录（快照覆盖）', (rt && rt.native && rt.native.mods_dir) || '—'],
        ['存档目录', (rt && rt.native && rt.native.saves_dir) || '—'],
        ['需要 .NET 运行时', '否（自包含）'],
      ]),
    ]),
    card('显示与访问', [
      nat('display_number', '显示编号（:N）', n.display_number, 'int',
        '同一台 NAS 上多个 X 会话需要不同编号'),
      nat('window_manager', '窗口管理器', n.window_manager, 'select',
        '游戏需要窗口管理器才能正常建窗（管理器已自动注入 XDG_DATA_DIRS）',
        [['openbox', 'openbox'], ['matchbox-window-manager', 'matchbox'],
         ['twm', 'twm'], ['fluxbox', 'fluxbox'], ['jwm', 'jwm']]),
      nat('xstack_source', '显示组件来源', n.xstack_source, 'select',
        '「应用内捆绑」用 apt-get download + dpkg -x 解到应用数据目录，不修改系统包状态',
        [['bundled', '应用内捆绑（推荐，不改系统）'], ['system', '系统已装（需自行安装）']]),
      nat('software_gl', '软件渲染回退', n.software_gl !== false, 'bool',
        'NAS 通常没有 GPU；开启后使用 Mesa 软件渲染（实测可用）'),
    ]),
  ]));

  root.appendChild(el('div', { class: 'grid c2 mb' }, [
    card('端口与监听', [
      nat('game_port', '游戏联机端口（UDP）', n.game_port, 'int', '玩家通过该端口联机'),
      nat('webvnc_port', 'WebVNC 端口（noVNC）', n.webvnc_port, 'int',
        'noVNC 本身没有认证，建议保持仅本机'),
      nat('vnc_port', 'VNC 端口', n.vnc_port, 'int', '使用管理器生成的随机密码'),
      nat('cmdwebui_port', 'CommandWebUI 端口', n.cmdwebui_port, 'int',
        '该 MOD 的 WebSocket 无认证'),
      nat('host_bind', '监听范围', n.host_bind, 'select',
        '游戏端口如需外网联机请选「所有网络接口」；WebVNC/VNC/CommandWebUI 会一并开放',
        [['127.0.0.1', '仅本机（推荐）'], ['0.0.0.0', '所有网络接口']]),
      nat('vnc_password', 'VNC 密码（恰好 8 位可见 ASCII）', n.vnc_password, 'str'),
      el('p', { class: 'hint', text: meta.port_hints || '' }),
    ]),
    card('运行参数', [
      nat('screen_width', '屏幕宽度', n.screen_width, 'int'),
      nat('screen_height', '屏幕高度', n.screen_height, 'int'),
      nat('screen_depth', '色彩深度', n.screen_depth, 'int'),
      nat('graceful_stop_seconds', '优雅停止等待（秒）', n.graceful_stop_seconds, 'int',
        '停止时先发 SIGTERM 等游戏收尾存档，超时后强制结束'),
      nat('extra_args', '额外启动参数（留空不加）', n.extra_args, 'str',
        '会原样附加到 StardewModdingAPI 命令行；请确认参数确实受支持'),
    ]),
  ]));

  root.appendChild(el('div', { class: 'grid c2 mb' }, [
    card('备份与保留策略', [
      pol('keep_auto_snapshots', '自动快照保留数量', p.keep_auto_snapshots, 'int'),
      pol('keep_manual_snapshots', '手动快照保留数量', p.keep_manual_snapshots, 'int'),
      pol('auto_snapshot_interval_minutes', '自动快照间隔（分钟）', p.auto_snapshot_interval_minutes, 'int'),
      pol('min_free_space_mb', '磁盘空间下限（MiB）', p.min_free_space_mb, 'int',
        '低于此值时拒绝执行高风险变更'),
      pol('snapshot_space_estimate_mb', '单次快照预留（MiB）', p.snapshot_space_estimate_mb, 'int'),
    ]),
    card('运行与恢复策略', [
      pol('auto_snapshot_enabled', '启用自动快照', p.auto_snapshot_enabled, 'bool'),
      pol('supervisor_enabled', '启用后台监管（异常退出自动恢复）', p.supervisor_enabled, 'bool'),
      pol('auto_start_with_nas', '随 NAS 启动自动拉起', p.auto_start_with_nas, 'bool'),
      pol('start_retry_max', '自动恢复最大重试次数', p.start_retry_max, 'int'),
      pol('start_retry_backoff_seconds', '首次退避秒数（之后指数增长）', p.start_retry_backoff_seconds, 'int'),
      nat('display_backend', '显示后端', n.display_backend || 'xvfb', 'select',
          '核显（Weston + Xwayland）与软件渲染（Xvfb）的**实测对比**：'
          + '核显约 109% CPU、33s 就绪；软件约 290% CPU、14-19s 就绪。'
          + '核显模式会忽略「强制软件渲染」开关。',
          [['xvfb', 'Xvfb —— 软件渲染（兼容性最好，推荐先用这个）'],
           ['weston', 'Weston + Xwayland —— 用核显（省 CPU，首次启动更慢）']]),
      pol('confirm_destructive', '破坏性操作要求二次确认', p.confirm_destructive, 'bool',
          '关闭后界面不再弹确认框（需输入指定文字才放行的少数操作仍会确认）；'
          + '服务端的自动快照、校验等安全措施不受影响'),
    ]),
  ]));

  root.appendChild(el('div', { class: 'btn-row mb' }, [
    el('button', { class: 'btn btn-primary', text: '保存并应用', onclick: save }),
    el('button', { class: 'btn', text: '放弃修改', onclick: () => refresh() }),
  ]));

  root.appendChild(card('数据目录', [
    kv([
      ['数据根目录', meta.data_root || '—'],
      ['应用家目录（升级保留）', meta.share_path || '—'],
      ['磁盘可用', (meta.disk_free_mb || 0) + ' MiB'],
      ['管理器面板端口', String(meta.panel_port || '')],
    ]),
    el('p', { class: 'hint mt', text: '存档、MOD、快照与版本档案都在应用家目录下，'
      + '升级或卸载（默认）不会删除。' }),
  ]));

  async function save() {
    const changes = { native: {}, policy: {} };
    const cur = { native: n, policy: p };
    const impactList = [];
    for (const [key, def] of Object.entries(fields)) {
      const [section, name] = key.split('.');
      let value;
      if (def.kind === 'bool') value = def.input.checked;
      else if (def.kind === 'int') {
        value = parseInt(def.input.value, 10);
        if (isNaN(value)) { toast(name + ' 必须是整数', 'err'); return; }
      } else value = def.input.value;
      if (JSON.stringify(value) !== JSON.stringify(cur[section][name])) {
        changes[section][name] = value;
        impactList.push(section + '.' + name + '：' + String(cur[section][name])
          + ' → ' + String(value));
      }
    }
    if (!Object.keys(changes.native).length && !Object.keys(changes.policy).length) {
      toast('没有检测到修改'); return;
    }
    const restartKeys = ['game_port', 'webvnc_port', 'vnc_port', 'cmdwebui_port',
      'host_bind', 'display_number', 'screen_width', 'screen_height', 'screen_depth',
      'window_manager', 'xstack_source', 'vnc_password'];
    if (Object.keys(changes.native).some((k) => restartKeys.includes(k))) {
      impactList.push('端口或显示参数的变更会重启游戏与显示栈（在线玩家会断开）。');
    }
    const ok = await confirmRisk({
      title: '应用配置变更',
      action: '写入配置并重启运行环境',
      impacts: ['执行前会自动创建快照。',
        '配置会先经过严格校验（显示组件、游戏本体、端口冲突）；不合法时会回滚配置文件。',
        ...impactList],
      danger: true,
    });
    if (!ok) return;
    const payload = {};
    if (Object.keys(changes.native).length) payload.native = changes.native;
    if (Object.keys(changes.policy).length) payload.policy = changes.policy;
    try {
      const res = await api('config', { json: { changes: payload } });
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
    metric('游戏进程', (info.runtime_status || {}).running ? '运行中' : '已停止',
      (info.runtime_status || {}).exists ? '原生部署' : '尚未安装'),
  ]));

  root.appendChild(el('div', { class: 'btn-row mb' }, [
    el('button', { class: 'btn btn-primary', text: '生成诊断包', onclick: makeBundle }),
    el('button', { class: 'btn', text: '安装 / 检查显示组件', onclick: installXstack }),
    el('button', { class: 'btn', text: '上传游戏本体', onclick: uploadGameFiles }),
    el('button', { class: 'btn', text: '获取 / 更新 SMAPI', onclick: installSmapi }),
    el('button', { class: 'btn btn-primary', text: 'Steam 扫码获取游戏本体', onclick: steamFetch }),
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
    card('原生运行环境', info.runtime ? kv([
      ['部署方式', '原生部署'],
      ['显示组件就绪', (info.runtime.xstack || {}).ok ? '是' : '否'],
      ['显示组件来源', (info.runtime.xstack || {}).installed ? '应用内捆绑' : '缺失'],
      ['游戏启动器', (info.runtime.game || {}).launcher_present ? '已放入' : '未放入'],
      ['游戏本体目录', (info.runtime.game || {}).root || '—'],
      ['需要 .NET 运行时', (info.runtime.game || {}).dotnet_runtime_required ? '是' : '否（自包含）'],
    ]) : el('p', { class: 'muted', text: '无数据' }),
      (info.runtime && (info.runtime.guidance || []).length)
        ? el('div', { class: 'banner warn mt' },
            (info.runtime.guidance || []).map((g) => el('div', { class: 'small', text: g })))
        : null),
  ]));

  const tabs = el('div', { class: 'tabs' }, [
    ...['manager', 'task', 'container'].map((src) => el('button', {
      text: src === 'manager' ? '管理器日志' : (src === 'task' ? '任务日志' : 'SMAPI 控制台'),
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

  root.appendChild(card('部署方式与审计基线', [
    kv([
      ['部署方式', (d.deployment && d.deployment.mode) || '原生部署'],
      ['说明', (d.deployment && d.deployment.note) || ''],
      ['显示栈', (d.deployment && d.deployment.display_stack) || ''],
      ['.NET 运行时', (d.deployment && d.deployment.dotnet) || ''],
      ['上游基线游戏版本', (d.audit_reference || {}).upstream_game_version || '—'],
      ['上游基线 SMAPI 版本', (d.audit_reference || {}).upstream_smapi_version || '—'],
      ['审计说明', (d.audit_reference || {}).note || ''],
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
