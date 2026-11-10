/* TeamFlow · AI 工作台（/{slug}/ai）：问候语 + 大输入框 + 快捷动作 + 最近会话 */

import { api } from '../api.js';
import { S } from '../store.js';
import { esc, toast, confirmAction, emptyHTML } from '../ui.js';
import { navigate } from '../router.js';
import { openIssueDialog } from '../issue-form.js';

const MODE_FALLBACK = [
  { value: 'ask', label: '问答' },
  { value: 'plan', label: '规划' },
  { value: 'review', label: '复盘' },
];

const QUICK_ACTIONS = [
  {
    key: 'risk',
    icon: 'alert',
    label: '排查风险',
    prompt: '帮我排查这个项目当前最大的风险，按影响从大到小排序，每条给出可执行的下一步。',
  },
  {
    key: 'week',
    icon: 'calendar',
    label: '规划本周',
    prompt: '帮我安排本周计划：先做哪些议题、哪些可以往后放？给出理由和依赖关系。',
  },
  { key: 'issue', icon: 'issue-opened', label: '新建议题' },
  { key: 'project', icon: 'repo', label: '起草新项目' },
  { key: 'engine', icon: 'rocket', label: '引擎洞察' },
];

const state = {
  slug: '',
  status: null,
  sessions: [],
  session: null,
  messages: [],
  mode: 'ask',
  context: [],
  issues: null,
  busy: false,
};

export async function render(view, route) {
  if (state.slug !== route.slug) {
    state.slug = route.slug;
    state.sessions = [];
    state.session = null;
    state.messages = [];
    state.context = [];
    state.issues = null;
    state.mode = 'ask';
  }
  view.innerHTML = pageHTML();
  bind(view);
  await Promise.all([loadStatus(view), state.session ? null : loadSessions(view)]);
}

/* ---------------- 数据结构 ---------------- */

function greeting() {
  const hour = new Date().getHours();
  if (hour < 6) return '夜深了';
  if (hour < 12) return '早上好';
  if (hour < 14) return '中午好';
  if (hour < 18) return '下午好';
  return '晚上好';
}

function who() {
  const user = S.user;
  if (user && (user.display_name || user.username)) return user.display_name || user.username;
  return '访客';
}

function modeLabel() {
  const modes = state.status?.modes?.length ? state.status.modes : MODE_FALLBACK;
  return (modes.find((item) => item.value === state.mode) || modes[0]).label;
}

function teamName() {
  return S.team?.name || state.slug;
}

function inSession() {
  return state.messages.length > 0 || Boolean(state.session);
}

/* ---------------- 结构 ---------------- */

function pageHTML() {
  return `
    <div class="ai-work">
      <div class="ai-work__stage" id="ai-stage">${state.session ? '' : heroHTML()}</div>
      ${composerHTML()}
      <div class="ai-work__actions" id="ai-actions">${actionsHTML()}</div>
      <section class="ai-recent" id="ai-recent">${sessionsHTML()}</section>
    </div>`;
}

function heroHTML() {
  return `
    <div class="ai-hero">
      <div class="ai-hero__mark"><gh-icon name="sparkle" size="20"></gh-icon></div>
      <h1 class="ai-hero__greet">${greeting()}，${esc(who())}</h1>
      <p class="ai-hero__sub">想看点什么？我可以读团队数据、拆任务、写周计划，或者按你的描述起草一个新项目组。</p>
    </div>`;
}

function composerHTML() {
  return `
    <div class="ai-composer" id="ai-composer">
      <div class="ai-composer__context" id="ai-context"></div>
      <textarea class="ai-composer__input" id="ai-input" rows="1"
        placeholder="问点什么，或者输入 @ 引用议题…"></textarea>
      <div class="ai-composer__bar">
        <div class="ai-composer__left">
          <button type="button" class="ai-pill" data-act="mode">
            <gh-icon name="comment" size="14"></gh-icon>
            <span class="ai-pill__text" id="ai-mode">${esc(modeLabel())}</span>
            <gh-icon name="triangle-down" size="12"></gh-icon>
          </button>
          <button type="button" class="ai-pill" data-act="scope">
            <gh-icon name="repo" size="14"></gh-icon>
            <span class="ai-pill__text">当前团队 · ${esc(teamName())}</span>
          </button>
          <button type="button" class="ai-pill ai-pill--icon" data-act="pick" title="引用议题">
            <gh-icon name="plus" size="14"></gh-icon>
          </button>
        </div>
        <div class="ai-composer__right">
          <span class="ai-pill ai-pill--ghost" title="接口与模型在「设置 → AI 助手」里配置">
            <gh-icon name="git-branch" size="14"></gh-icon>
            <span class="ai-pill__text" id="ai-model">自动</span>
          </span>
          <span class="ai-pill ai-pill--ghost"><gh-icon name="sliders" size="14"></gh-icon>
            <span class="ai-pill__text">平衡优先</span></span>
          <button type="button" class="ai-icon-btn" data-act="history" title="最近会话">
            <gh-icon name="history" size="16"></gh-icon>
          </button>
          <button type="button" class="ai-send" data-act="send" title="发送（Enter）">
            <gh-icon name="paper-airplane" size="16"></gh-icon>
          </button>
        </div>
      </div>
    </div>`;
}

function actionsHTML() {
  return QUICK_ACTIONS.map(
    (action) => `
      <button type="button" class="ai-action" data-quick="${action.key}">
        <gh-icon name="${action.icon}" size="14"></gh-icon>
        <span>${esc(action.label)}</span>
      </button>`,
  ).join('');
}

function sessionsHTML() {
  if (!state.sessions.length) {
    return `
      <div class="ai-recent__head">最近会话</div>
      ${emptyHTML({ icon: 'comment-discussion', title: '还没有会话',
        desc: '问第一个问题就会自动生成一段会话，可以随时回来继续。' })}`;
  }
  return `
    <div class="ai-recent__head">最近会话</div>
    <div class="ai-recent__list">
      ${state.sessions.map((row) => `
        <div class="ai-session" data-session="${row.id}" role="button" tabindex="0">
          <div class="ai-session__icon"><gh-icon name="comment-discussion" size="15"></gh-icon></div>
          <div class="ai-session__main">
            <div class="ai-session__title">${esc(row.title)}</div>
            <div class="ai-session__meta">
              <span>${esc(row.mode_label || '问答')}</span>
              <span>· ${row.message_count} 条消息</span>
              <span>· ${esc(relativeTime(row.updated_at))}</span>
            </div>
          </div>
          <button type="button" class="ai-icon-btn ai-session__del" data-act="del-session"
            data-id="${row.id}" title="删除会话"><gh-icon name="trash" size="14"></gh-icon></button>
        </div>`).join('')}
    </div>`;
}

function relativeTime(value) {
  if (!value) return '刚刚';
  const stamp = new Date(value).getTime();
  if (!Number.isFinite(stamp)) return '刚刚';
  const diff = Math.max(0, Date.now() - stamp);
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.floor(hours / 24)} 天前`;
}

function threadHTML() {
  const header = `
    <div class="ai-thread__head">
      <button type="button" class="ai-link" data-act="back">
        <gh-icon name="chevron-left" size="14"></gh-icon> 最近会话
      </button>
      <span class="ai-thread__title">${esc(state.session?.title || '新的对话')}</span>
      <span class="ai-thread__mode">${esc(modeLabel())}</span>
    </div>`;
  const messages = state.messages.map((message) => bubbleHTML(message)).join('');
  const pending = state.busy
    ? `<div class="ai-turn ai-turn--bot"><div class="ai-turn__avatar"><gh-icon name="sparkle" size="13"></gh-icon></div>
        <div class="ai-turn__bubble ai-turn__bubble--pending"><gh-spinner></gh-spinner> 正在读团队数据…</div></div>`
    : '';
  return `<div class="ai-thread">${header}<div class="ai-thread__list" id="ai-thread-list">${messages}${pending}</div></div>`;
}

function bubbleHTML(message) {
  const chips = (message.context || []).length
    ? `<div class="ai-turn__refs">${message.context
      .map((number) => `<span class="ai-turn__ref">#${number}</span>`).join('')}</div>`
    : '';
  if (message.role === 'user') {
    return `
      <div class="ai-turn ai-turn--user">
        <div class="ai-turn__bubble">${esc(message.content)}${chips}</div>
      </div>`;
  }
  const html = typeof GHUI !== 'undefined' && GHUI.md
    ? GHUI.md(message.content || '')
    : esc(message.content || '');
  return `
    <div class="ai-turn ai-turn--bot">
      <div class="ai-turn__avatar"><gh-icon name="sparkle" size="13"></gh-icon></div>
      <div class="ai-turn__bubble"><div class="ai-turn__md gh-md">${html}</div>${chips}</div>
    </div>`;
}

/* ---------------- 数据 ---------------- */

async function loadStatus(view) {
  try {
    state.status = await api.get(`/api/teams/${state.slug}/ai`);
  } catch (error) {
    state.status = { configured: false, model: '', modes: MODE_FALLBACK };
  }
  const model = view.querySelector('#ai-model');
  if (model) {
    model.textContent = state.status.configured ? (state.status.model || '默认模型') : '未配置';
  }
  const mode = view.querySelector('#ai-mode');
  if (mode) mode.textContent = modeLabel();
}

async function loadSessions(view) {
  try {
    const data = await api.get(`/api/teams/${state.slug}/ai/sessions`);
    state.sessions = data.sessions || [];
  } catch (error) {
    state.sessions = [];
  }
  const recent = view.querySelector('#ai-recent');
  if (recent) recent.innerHTML = sessionsHTML();
}

async function openSession(view, id) {
  try {
    const data = await api.get(`/api/teams/${state.slug}/ai/sessions/${id}`);
    state.session = data.session;
    state.messages = data.messages || [];
    state.mode = data.session.mode || 'ask';
    paintStage(view);
  } catch (error) {
    toast(error.message || '会话打不开', 'err');
  }
}

async function refreshSessions(view) {
  try {
    const data = await api.get(`/api/teams/${state.slug}/ai/sessions`);
    state.sessions = data.sessions || [];
  } catch (error) {
    /* 列表刷新失败不影响对话 */
  }
  const recent = view.querySelector('#ai-recent');
  if (recent) recent.innerHTML = sessionsHTML();
}

/* ---------------- 渲染与交互 ---------------- */

function paintStage(view) {
  const stage = view.querySelector('#ai-stage');
  if (!stage) return;
  stage.innerHTML = inSession() ? threadHTML() : heroHTML();
  const actions = view.querySelector('#ai-actions');
  if (actions) actions.hidden = inSession();
  const recent = view.querySelector('#ai-recent');
  if (recent) recent.hidden = inSession();
  paintContext(view);
  scrollThread(view);
}

function scrollThread(view) {
  const list = view.querySelector('#ai-thread-list');
  if (list) list.scrollTop = list.scrollHeight;
}

function paintContext(view) {
  const box = view.querySelector('#ai-context');
  if (!box) return;
  box.innerHTML = state.context
    .map((item) => `
      <span class="ai-ctx">
        <gh-icon name="issue-opened" size="12"></gh-icon>
        <span>#${item.number} ${esc(item.title)}</span>
        <button type="button" data-act="drop-ctx" data-number="${item.number}"
          title="移除引用">×</button>
      </span>`)
    .join('');
}

function bind(view) {
  view.addEventListener('click', (event) => onClick(view, event));
  view.addEventListener('keydown', (event) => onKey(view, event));
  const input = view.querySelector('#ai-input');
  if (input) {
    input.addEventListener('input', () => autoGrow(input));
    input.addEventListener('keydown', (event) => {
      if (event.key === '@' && !state.busy) {
        setTimeout(() => openPicker(view), 0);
      }
    });
  }
}

function onKey(view, event) {
  if (event.target?.id !== 'ai-input') return;
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    send(view);
  }
}

function autoGrow(input) {
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 220)}px`;
}

function onClick(view, event) {
  const trigger = event.target.closest('[data-act], [data-quick], [data-session]');
  if (!trigger) return;
  const act = trigger.dataset.act;
  if (trigger.dataset.session) {
    openSession(view, Number(trigger.dataset.session));
    return;
  }
  if (trigger.dataset.quick) {
    runQuick(view, trigger.dataset.quick);
    return;
  }
  switch (act) {
    case 'send':
      send(view);
      break;
    case 'mode':
      openMenu(view, trigger, modeOptions(), (value) => {
        state.mode = value;
        const label = view.querySelector('#ai-mode');
        if (label) label.textContent = modeLabel();
      });
      break;
    case 'scope':
      toast('当前版本按单个团队检索数据', 'ok');
      break;
    case 'pick':
      openPicker(view);
      break;
    case 'history':
      view.querySelector('#ai-recent')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      break;
    case 'back':
      state.session = null;
      state.messages = [];
      paintStage(view);
      refreshSessions(view);
      break;
    case 'drop-ctx':
      state.context = state.context.filter((item) => item.number !== Number(trigger.dataset.number));
      paintContext(view);
      break;
    case 'del-session':
      event.stopPropagation();
      deleteSession(view, Number(trigger.dataset.id));
      break;
    default:
      break;
  }
}

async function deleteSession(view, id) {
  const ok = await confirmAction({
    title: '删除这段会话',
    message: '会话与消息会一起删除，不能恢复。',
    okText: '删除',
    danger: true,
  });
  if (!ok) return;
  try {
    await api.del(`/api/teams/${state.slug}/ai/sessions/${id}`);
    if (state.session?.id === id) {
      state.session = null;
      state.messages = [];
      paintStage(view);
    }
    toast('会话已删除');
    refreshSessions(view);
  } catch (error) {
    toast(error.message || '删除失败', 'err');
  }
}

function modeOptions() {
  return state.status?.modes?.length ? state.status.modes : MODE_FALLBACK;
}

function openMenu(view, anchor, options, onPick) {
  view.querySelector('.ai-menu')?.remove();
  const menu = document.createElement('div');
  menu.className = 'ai-menu';
  menu.innerHTML = options
    .map((option) => `
      <button type="button" class="ai-menu__item" data-value="${esc(option.value)}">
        <span>${esc(option.label)}</span>
        <gh-icon name="check" size="13" class="ai-menu__check"></gh-icon>
      </button>`)
    .join('');
  anchor.parentElement.appendChild(menu);
  menu.addEventListener('click', (event) => {
    const item = event.target.closest('[data-value]');
    if (!item) return;
    onPick(item.dataset.value);
    menu.remove();
  });
  setTimeout(() => {
    const close = (event) => {
      if (!menu.contains(event.target)) {
        menu.remove();
        document.removeEventListener('click', close, true);
      }
    };
    document.addEventListener('click', close, true);
  }, 0);
}

/* ---------------- @ 引用议题 ---------------- */

async function loadIssues() {
  if (state.issues) return state.issues;
  try {
    const data = await api.get(`/api/teams/${state.slug}/issues`, { state: 'open', limit: 120 });
    state.issues = (data.issues || []).map((issue) => ({
      number: issue.number, title: issue.title, status: issue.status,
    }));
  } catch (error) {
    state.issues = [];
  }
  return state.issues;
}

async function openPicker(view) {
  const issues = await loadIssues();
  const box = view.querySelector('#ai-composer');
  if (!box) return;
  view.querySelector('.ai-picker')?.remove();
  const picker = document.createElement('div');
  picker.className = 'ai-picker';
  picker.innerHTML = `
    <div class="ai-picker__head">
      <gh-icon name="issue-opened" size="13"></gh-icon>
      <span>引用议题（最多 8 个）</span>
    </div>
    <input class="ai-picker__search" placeholder="搜索议题编号或标题" />
    <div class="ai-picker__list" id="ai-picker-list"></div>`;
  box.appendChild(picker);

  const list = picker.querySelector('#ai-picker-list');
  const search = picker.querySelector('.ai-picker__search');
  const paint = () => {
    const query = String(search.value || '').trim().toLowerCase();
    const rows = issues.filter((issue) => {
      if (!query) return true;
      return String(issue.number).includes(query) || issue.title.toLowerCase().includes(query);
    }).slice(0, 30);
    list.innerHTML = rows.length
      ? rows.map((issue) => `
        <button type="button" class="ai-picker__row" data-number="${issue.number}">
          <span class="ai-picker__num">#${issue.number}</span>
          <span class="ai-picker__title">${esc(issue.title)}</span>
        </button>`).join('')
      : '<div class="ai-picker__empty">没有匹配的议题</div>';
  };
  paint();
  search.addEventListener('input', paint);
  search.focus();
  list.addEventListener('click', (event) => {
    const row = event.target.closest('[data-number]');
    if (!row) return;
    addContext(Number(row.dataset.number));
    picker.remove();
    const input = view.querySelector('#ai-input');
    const issue = issues.find((item) => item.number === Number(row.dataset.number));
    if (input && issue && !input.value.includes(`#${issue.number}`)) {
      input.value = `${input.value}${input.value && !input.value.endsWith(' ') ? ' ' : ''}#${issue.number} `;
      autoGrow(input);
      input.focus();
    }
    paintContext(view);
  });
  setTimeout(() => {
    const close = (event) => {
      if (!picker.contains(event.target)) {
        picker.remove();
        document.removeEventListener('click', close, true);
      }
    };
    document.addEventListener('click', close, true);
  }, 0);
}

function addContext(number) {
  if (state.context.some((item) => item.number === number)) return;
  if (state.context.length >= 8) {
    toast('最多引用 8 个议题', 'err');
    return;
  }
  const issue = (state.issues || []).find((item) => item.number === number);
  state.context.push({ number, title: issue?.title || '' });
}

/* ---------------- 发送 ---------------- */

function runQuick(view, key) {
  if (key === 'issue') {
    openIssueDialog({ slug: state.slug, onSaved: () => {} });
    return;
  }
  if (key === 'project') {
    navigate('/teams/new');
    return;
  }
  if (key === 'engine') {
    navigate(`/${state.slug}/engine`);
    return;
  }
  const action = QUICK_ACTIONS.find((item) => item.key === key);
  if (!action) return;
  const input = view.querySelector('#ai-input');
  if (input) {
    input.value = action.prompt;
    autoGrow(input);
  }
  send(view);
}

async function send(view) {
  if (state.busy) return;
  const input = view.querySelector('#ai-input');
  const text = String(input?.value || '').trim();
  if (!text) {
    input?.focus();
    return;
  }
  state.busy = true;
  state.messages.push({ role: 'user', content: text, context: state.context.map((i) => i.number) });
  if (input) {
    input.value = '';
    autoGrow(input);
  }
  paintStage(view);
  try {
    const result = await api.post(`/api/teams/${state.slug}/ai/chat`, {
      message: text,
      session_id: state.session?.id || null,
      context: state.context.map((item) => item.number),
      mode: state.mode,
    });
    if (result.session_id && !state.session) {
      state.session = { id: result.session_id, title: result.title || text, mode: result.mode };
    }
    state.messages.push({ role: 'assistant', content: result.reply });
    state.context = [];
  } catch (error) {
    state.messages.push({
      role: 'assistant',
      content: `抱歉，这次没有拿到回复：${error.message || '请稍后重试'}`,
    });
  } finally {
    state.busy = false;
    paintStage(view);
    refreshSessions(view);
  }
}
