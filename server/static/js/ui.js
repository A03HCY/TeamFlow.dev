/* TeamFlow · 通用渲染片段与交互辅助（基于 gh-ui 组件） */

import { S } from './store.js';

/* ---------------- 转义 ---------------- */

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

export function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value).replace(/[&<>"']/g, (ch) => ESCAPES[ch]);
}

export const attr = esc;

export function h(html) {
  const tpl = document.createElement('template');
  tpl.innerHTML = String(html).trim();
  return tpl.content.firstElementChild;
}

export function frag(html) {
  const tpl = document.createElement('template');
  tpl.innerHTML = String(html).trim();
  return tpl.content;
}

/* 页面级事件绑定：同一个宿主节点上每类事件只保留最后一次注册的处理器，
   避免页面反复渲染（刷新 / 重进）时叠加重复监听。 */
const viewBindings = new WeakMap();

export function onView(node, type, handler) {
  if (!node || typeof handler !== 'function') return;
  const bound = viewBindings.get(node) || {};
  if (bound[type]) node.removeEventListener(type, bound[type]);
  bound[type] = handler;
  viewBindings.set(node, bound);
  node.addEventListener(type, handler);
}

/* ---------------- 文本 / 日期 ---------------- */

export function initials(name) {
  const text = String(name || '').trim();
  if (!text) return '?';
  const cjk = text.match(/[\u4e00-\u9fa5]/);
  if (cjk) return text.slice(0, 2);
  const parts = text.split(/[\s._-]+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return text.slice(0, 2).toUpperCase();
}

function parseTime(value) {
  if (!value) return NaN;
  if (typeof value === 'number') return value;
  const text = String(value);
  const iso = text.includes('T') ? text : text.replace(' ', 'T');
  return Date.parse(iso.endsWith('Z') || /[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`);
}

export function timeAgo(value) {
  const t = parseTime(value);
  if (!Number.isFinite(t)) return '';
  const diff = Date.now() - t;
  const sec = Math.round(diff / 1000);
  if (sec < 45) return '刚刚';
  const min = Math.round(sec / 60);
  if (min < 60) return `${min} 分钟前`;
  const hour = Math.round(min / 60);
  if (hour < 24) return `${hour} 小时前`;
  const day = Math.round(hour / 24);
  if (day < 30) return `${day} 天前`;
  return fmtDate(value);
}

export function fmtDate(value) {
  if (!value) return '—';
  const text = String(value).slice(0, 10);
  return /^\d{4}-\d{2}-\d{2}$/.test(text) ? text : String(value);
}

export function fmtDateTime(value) {
  const t = parseTime(value);
  if (!Number.isFinite(t)) return '—';
  const d = new Date(t);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function todayISO() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function addDaysISO(iso, days) {
  const base = new Date(`${iso}T00:00:00`);
  base.setDate(base.getDate() + days);
  const pad = (n) => String(n).padStart(2, '0');
  return `${base.getFullYear()}-${pad(base.getMonth() + 1)}-${pad(base.getDate())}`;
}

export const WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];

export function weekdayName(index) {
  const i = Number(index);
  return Number.isFinite(i) && WEEKDAYS[i] ? WEEKDAYS[i] : '—';
}

export function fmtWeek(week) {
  if (!week) return '未排期';
  const m = String(week).match(/^(\d{4})-W(\d{1,2})$/);
  if (!m) return String(week);
  return `${m[1]} 第 ${Number(m[2])} 周`;
}

export function daysBetween(a, b) {
  const ta = parseTime(a);
  const tb = parseTime(b);
  if (!Number.isFinite(ta) || !Number.isFinite(tb)) return 0;
  return Math.round((tb - ta) / 86400000);
}

export function isOverdue(dueDate) {
  if (!dueDate) return false;
  return String(dueDate).slice(0, 10) < todayISO();
}

/* ---------------- 头像 / 标签 / 徽章 ---------------- */

export function avatarHTML(person, size = 24, extra = '') {
  const name = (person && (person.display_name || person.name || person.username)) || '?';
  const src = person && person.avatar_url ? person.avatar_url : '';
  const title = name ? ` title="${esc(name)}"` : '';
  if (src) return `<gh-avatar size="${size}" src="${esc(src)}"${title}${extra}></gh-avatar>`;
  return `<gh-avatar size="${size}"${title}${extra}>${esc(initials(name))}</gh-avatar>`;
}

export function labelChips(labels, limit = 0) {
  const list = Array.isArray(labels) ? labels : [];
  const shown = limit > 0 ? list.slice(0, limit) : list;
  const more = limit > 0 && list.length > limit ? `<gh-label>+${list.length - limit}</gh-label>` : '';
  return shown.map((label) => `<gh-label color="${esc(label.color || '#6b7280')}">${esc(label.name)}</gh-label>`).join('') + more;
}

export const STATUS = {
  todo: { label: '待办', tone: 'open' },
  in_progress: { label: '进行中', tone: 'accent' },
  done: { label: '已完成', tone: 'closed' },
};

export function statusBadge(status, dot = true) {
  const item = STATUS[status] || STATUS.todo;
  return `<gh-badge state="${item.tone}"${dot ? ' dot' : ''}>${item.label}</gh-badge>`;
}

export const PRIORITY = {
  high: { label: '高优先级', short: '高', tone: 'danger' },
  medium: { label: '中优先级', short: '中', tone: 'warn' },
  low: { label: '低优先级', short: '低', tone: 'open' },
};

export function priorityBadge(priority, short = false) {
  const item = PRIORITY[priority] || PRIORITY.medium;
  return `<gh-badge state="${item.tone}">${short ? item.short : item.label}</gh-badge>`;
}

export function stateBadge(state) {
  return state === 'closed'
    ? '<gh-badge state="closed" dot>已关闭</gh-badge>'
    : '<gh-badge state="open" dot>开放中</gh-badge>';
}

/* ---------------- 议题行 ---------------- */

export function issueRow(issue, options = {}) {
  const slug = options.slug || (S.detail && S.detail.slug) || '';
  const closed = issue.state === 'closed';
  const assignee = issue.assignee_id
    ? { display_name: issue.assignee_name, avatar_url: issue.assignee_avatar_url }
    : null;
  const labels = labelChips(issue.labels, options.labelLimit || 4);
  const comments = issue.comment_count
    ? `<span class="row-tight"><gh-icon name="comment" size="12"></gh-icon>${issue.comment_count}</span>`
    : '';
  const meta = [
    `<span class="mono">#${esc(issue.number)}</span>`,
    statusBadge(issue.status, false),
    issue.milestone_title ? `<span class="row-tight"><gh-icon name="milestone" size="12"></gh-icon>${esc(issue.milestone_title)}</span>` : '',
    issue.plan_week ? `<span class="row-tight"><gh-icon name="calendar" size="12"></gh-icon>${esc(fmtWeek(issue.plan_week))}</span>` : '',
    issue.due_date ? `<span class="row-tight${isOverdue(issue.due_date) && !closed ? ' muted' : ''}"><gh-icon name="clock" size="12"></gh-icon>${esc(fmtDate(issue.due_date))}</span>` : '',
    comments,
    `<span>更新于 ${esc(timeAgo(issue.updated_at || issue.created_at))}</span>`,
  ].filter(Boolean).join('');

  const isChild = !!issue.parent_id;
  return `<div class="issue-row${isChild ? ' is-child' : ''}" data-number="${esc(issue.number)}">
    <span class="issue-row__lead${closed ? ' is-closed' : ''}${isChild ? ' is-child' : ''}"><gh-icon name="${closed ? 'issue-closed' : (isChild ? 'git-branch' : 'issue-opened')}" size="16"></gh-icon></span>
    <div class="issue-row__main">
      <a class="issue-row__title" href="/${esc(slug)}/issues/${esc(issue.number)}">${esc(issue.title)}</a>
      <div class="issue-row__meta">${meta}</div>
      ${labels ? `<div class="issue-row__labels">${labels}</div>` : ''}
    </div>
    <div class="issue-row__side">
      ${issue.estimate_hours ? `<span class="small muted mono">${esc(issue.estimate_hours)}h</span>` : ''}
      ${priorityBadge(issue.priority, true)}
      ${assignee ? avatarHTML(assignee, 22) : ''}
    </div>
  </div>`;
}

/* ---------------- 占位片段 ---------------- */

export const SPINNER = '<div style="display:flex;justify-content:center;padding:48px"><gh-spinner></gh-spinner></div>';

export function emptyHTML(options = {}) {
  const { icon = 'inbox', title = '暂无数据', desc = '' } = options;
  return `<gh-empty icon="${esc(icon)}" title="${esc(title)}">${esc(desc)}</gh-empty>`;
}

export function errorHTML(message, hint = '') {
  return `<div class="error-box">
    <gh-icon name="alert" size="24"></gh-icon>
    <div class="error-box__title">出错了</div>
    <div class="error-box__msg">${esc(message || '请求失败')}</div>
    ${hint ? `<div class="error-box__msg">${esc(hint)}</div>` : ''}
    <gh-btn size="sm" data-act="retry">重新加载</gh-btn>
  </div>`;
}

/* ---------------- 提示 / 确认 / 弹层 ---------------- */

// 同一条提示连续触发时不该在屏幕右侧叠成一列（连点按钮就会堆出十几条）。
// 3 秒内重复的同文案直接忽略，新的文案照常弹。
const TOAST_DEDUPE_MS = 3000;
const toastRecent = new Map();

export function toast(message, kind = 'ok') {
  const text = String(message);
  const now = Date.now();
  if (now - (toastRecent.get(text) || 0) < TOAST_DEDUPE_MS) return;
  toastRecent.set(text, now);
  if (toastRecent.size > 40) {
    for (const [key, at] of toastRecent) {
      if (now - at >= TOAST_DEDUPE_MS) toastRecent.delete(key);
    }
  }
  if (window.GHUI && typeof GHUI.toast === 'function') GHUI.toast(text, kind === 'err' ? 'err' : 'ok');
  else if (kind === 'err') console.error(message);
  else console.info(message);
}

export async function confirmAction(options = {}) {
  const { title = '确认操作', message = '', okText = '确定', danger = false } = options;
  if (window.GHUI && typeof GHUI.confirm === 'function') {
    try { return !!(await GHUI.confirm({ title, message, okText, danger })); } catch { return false; }
  }
  return window.confirm(`${title}\n\n${message}`);
}

export async function askText(options = {}) {
  const { title = '输入内容', label = '', value = '', okText = '确定', placeholder = '' } = options;
  if (window.GHUI && typeof GHUI.prompt === 'function') {
    try { return await GHUI.prompt({ title, label, value, okText, placeholder }); } catch { return null; }
  }
  return window.prompt(title, value);
}

let dialogSeq = 0;

export function openDialog(options = {}) {
  const {
    title = '',
    body = '',
    okText = '保存',
    cancelText = '取消',
    width = '',
    danger = false,
    onSubmit = null,
    onMount = null,
  } = options;
  const id = `tf-dlg-${++dialogSeq}`;
  const buttons = [];
  if (okText) buttons.push(`ok-text="${esc(okText)}"`);
  if (cancelText) buttons.push(`cancel-text="${esc(cancelText)}"`);
  if (danger) buttons.push('ok-variant="danger"');
  if (width) buttons.push(`width="${esc(width)}"`);
  const holder = document.createElement('div');
  holder.innerHTML = `<gh-dialog id="${id}" title="${esc(title)}" ${buttons.join(' ')}>${body}</gh-dialog>`;
  const dialog = holder.firstElementChild;
  document.body.appendChild(dialog);
  const destroy = () => { if (dialog.isConnected) dialog.remove(); };
  dialog.addEventListener('gh:confirm', (event) => {
    event.preventDefault();
    const values = readValues(dialog);
    if (!onSubmit) { dialog.close(); return; }
    Promise.resolve()
      .then(() => onSubmit(values, dialog))
      .then((result) => { if (result !== false) dialog.close(); })
      .catch((error) => toast((error && error.message) || '操作失败', 'err'));
  });
  dialog.addEventListener('gh:close', destroy);
  if (onMount) onMount(dialog);
  dialog.show();
  return dialog;
}

/* ---------------- 表单读取 / 填充 ---------------- */

const FIELD_SELECTOR = 'input[name], textarea[name], select[name], gh-input[name], gh-textarea[name], gh-select[name], gh-checkbox[name], gh-radio[name], gh-switch[name], gh-combobox[name], gh-tag-input[name], gh-segmented[name], gh-range[name]';

const NATIVE_FIELD_TAGS = new Set(['INPUT', 'TEXTAREA', 'SELECT']);

function fieldElements(root) {
  const all = Array.from(root.querySelectorAll(FIELD_SELECTOR));
  // gh-input / gh-textarea / gh-select 的外壳里包着一个原生控件，外壳自己的 .value 不会跟着
  // 用户输入变化。同一 name 同时存在外壳与原生控件时，一律取原生那个，否则读到的永远是空。
  const kept = all.filter((el) => {
    if (NATIVE_FIELD_TAGS.has(el.tagName)) return true;
    const inner = el.querySelector('input, textarea, select');
    return !(inner && inner.getAttribute('name') === el.getAttribute('name'));
  });
  return kept.filter((el) => !kept.some((other) => other !== el && other.contains(el)));
}

/** 兜底：外壳没有把 value 代理出来时，去它的子树里找原生控件的值。 */
function componentValue(el) {
  const outer = el.value;
  if (outer !== undefined && outer !== null && String(outer) !== '') return outer;
  const inner = el.querySelector('input, textarea, select');
  return inner ? inner.value : (outer === undefined || outer === null ? '' : outer);
}

export function readValues(root) {
  const out = {};
  const push = (name, value) => {
    if (!name) return;
    if (Object.prototype.hasOwnProperty.call(out, name)) {
      out[name] = [].concat(out[name], value);
    } else {
      out[name] = value;
    }
  };
  fieldElements(root).forEach((el) => {
    const name = el.getAttribute('name');
    if (!name) return;
    const tag = el.tagName;
    if (tag === 'INPUT') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'file') return;
      if (type === 'checkbox' || type === 'radio') {
        if (el.checked) push(name, el.value || 'on');
        return;
      }
      push(name, el.value);
      return;
    }
    if (tag === 'TEXTAREA' || tag === 'SELECT') { push(name, el.value); return; }
    if (tag === 'GH-CHECKBOX' || tag === 'GH-RADIO') {
      const input = el.querySelector('input');
      if (input && input.checked) push(name, el.getAttribute('value') || 'on');
      return;
    }
    if (tag === 'GH-SWITCH') {
      const input = el.querySelector('input');
      push(name, input ? input.checked : el.hasAttribute('checked'));
      return;
    }
    if (tag === 'GH-INPUT' || tag === 'GH-TEXTAREA' || tag === 'GH-COMBOBOX') {
      push(name, componentValue(el));
      return;
    }
    push(name, el.value);
  });
  return out;
}

export function applyValues(root, values = {}) {
  Object.keys(values).forEach((name) => {
    const nodes = fieldElements(root).filter((el) => el.getAttribute('name') === name);
    const el = nodes[nodes.length - 1];
    if (!el) return;
    const value = values[name];
    const tag = el.tagName;
    if (tag === 'GH-CHECKBOX' || tag === 'GH-RADIO' || tag === 'GH-SWITCH') {
      const on = !!value;
      el.toggleAttribute('checked', on);
      const input = el.querySelector('input');
      if (input) input.checked = on;
      return;
    }
    if ('value' in el) el.value = value === null || value === undefined ? '' : String(value);
  });
}

/* ---------------- 表单片段 ---------------- */

export function optionsHTML(options, selected) {
  const list = Array.isArray(options) ? options : [];
  return list.map((option) => {
    const value = String(option.value);
    const isSelected = selected !== undefined && selected !== null && String(selected) === value;
    return `<option value="${esc(value)}"${isSelected ? ' selected' : ''}>${esc(option.label)}</option>`;
  }).join('');
}

export function fieldHTML(spec = {}) {
  const {
    label = '', hint = '', name = '', type = 'text', placeholder = '',
    options = [], rows = 4, value = '', icon = '', maxlength = 0,
  } = spec;
  const labelAttr = label ? ` label="${esc(label)}"` : '';
  const hintAttr = hint ? ` hint="${esc(hint)}"` : '';
  if (type === 'select') {
    return `<gh-field${labelAttr}${hintAttr}><gh-select name="${esc(name)}">${optionsHTML(options, value)}</gh-select></gh-field>`;
  }
  if (type === 'textarea') {
    return `<gh-field${labelAttr}${hintAttr}><gh-textarea name="${esc(name)}" rows="${Number(rows) || 4}" placeholder="${esc(placeholder)}" value="${esc(value == null ? '' : value)}"></gh-textarea></gh-field>`;
  }
  if (type === 'switch') {
    return `<gh-field${labelAttr}${hintAttr}><gh-switch name="${esc(name)}" status${value ? ' checked' : ''}></gh-switch></gh-field>`;
  }
  if (type === 'checkbox') {
    return `<gh-field${hintAttr}><gh-checkbox name="${esc(name)}" value="${esc(spec.value || 'on')}"${value ? ' checked' : ''}>${esc(label)}</gh-checkbox></gh-field>`;
  }
  const parts = [`name="${esc(name)}"`, `type="${esc(type)}"`];
  if (placeholder) parts.push(`placeholder="${esc(placeholder)}"`);
  if (icon) parts.push(`icon="${esc(icon)}"`);
  if (maxlength) parts.push(`maxlength="${Number(maxlength)}"`);
  parts.push(`value="${esc(value == null ? '' : value)}"`);
  return `<gh-field${labelAttr}${hintAttr}><gh-input ${parts.join(' ')}></gh-input></gh-field>`;
}

/* gh-textarea 不读取 value 属性，插入 DOM 后需要手动补一次值 */
export function hydrateFormValues(root) {
  if (!root || !root.querySelectorAll) return;
  root.querySelectorAll('gh-textarea[value]').forEach((el) => {
    if (el.dataset.tfHydrated === '1') return;
    el.dataset.tfHydrated = '1';
    const text = el.getAttribute('value');
    if (text && 'value' in el) el.value = text;
  });
}

export function checkboxGroupHTML(name, items, selected = []) {
  const chosen = new Set((selected || []).map((v) => String(v)));
  const rows = (items || []).map((item) => `<gh-checkbox name="${esc(name)}" value="${esc(item.value)}"${chosen.has(String(item.value)) ? ' checked' : ''}>${esc(item.label)}</gh-checkbox>`).join('');
  return `<div class="chip-pick">${rows}</div>`;
}

/* ---------------- 小组件 ---------------- */

export function pageHead(title, desc, actionsHTML = '') {
  return `<gh-page-head title="${esc(title)}" desc="${esc(desc)}">${actionsHTML}</gh-page-head>`;
}

export function tag(text, color) {
  return `<gh-label${color ? ` color="${esc(color)}"` : ''}>${esc(text)}</gh-label>`;
}
