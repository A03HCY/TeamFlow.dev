/* TeamFlow · 常驻 AI 对话框（挂到 body，路由切换不重建） */

import { api, stream } from './api.js';
import { esc, toast } from './ui.js';
import { navigate } from './router.js';

const STORE_PREFIX = 'tf:ai-chat:';
const MAX_TURNS = 24;

const state = {
  mounted: false,
  open: false,
  busy: false,
  scope: 'global',
  slug: '',
  messages: [],
  configured: null,
  model: '',
  quick: [],
  error: '',
};

let root = null;

/* ------------------------------------------------------------ 持久化 */

function storageKey() {
  return STORE_PREFIX + (state.slug ? `team:${state.slug}` : 'global');
}

function loadMessages() {
  try {
    const raw = localStorage.getItem(storageKey());
    const parsed = raw ? JSON.parse(raw) : [];
    state.messages = Array.isArray(parsed) ? parsed.slice(-MAX_TURNS) : [];
  } catch {
    state.messages = [];
  }
}

function saveMessages() {
  try {
    localStorage.setItem(storageKey(), JSON.stringify(state.messages.slice(-MAX_TURNS)));
  } catch {
    /* 隐私模式下 localStorage 可能不可用，忽略 */
  }
}

/* ------------------------------------------------------------ 渲染 */

function messageHTML(item) {
  if (item.role === 'user') {
    return `<div class="ai-msg ai-msg--user"><div class="ai-msg__bubble">${esc(item.content)}</div></div>`;
  }
  const failed = item.error ? ' is-error' : '';
  return `<div class="ai-msg ai-msg--bot${failed}">
    <span class="ai-msg__avatar"><gh-icon name="sparkle" size="13"></gh-icon></span>
    <div class="ai-msg__bubble"><gh-md class="ai-msg__md"></gh-md></div>
  </div>`;
}

function messagesHTML() {
  if (!state.messages.length) {
    const quick = (state.quick || []).map(
      (item) => `<button class="ai-chip" data-quick="${esc(item.key || '')}">${esc(item.label)}</button>`
    ).join('');
    const hint = !state.slug
      ? '打开任意团队页面后，这里会带上该团队的实时进度。'
      : state.configured === false
        ? 'AI 还没有配置。到「设置 → AI 助手」填好接口地址、模型与密钥即可开聊。'
        : '可以问进度、让它拆任务、拟里程碑，或者写周报。';
    return `<div class="ai-empty">
      <gh-icon name="sparkle" size="22"></gh-icon>
      <div class="ai-empty__title">AI 助手</div>
      <div class="ai-empty__hint">${esc(hint)}</div>
      ${quick ? `<div class="ai-chips">${quick}</div>` : ''}
    </div>`;
  }
  return state.messages.map(messageHTML).join('');
}

function syncMessages() {
  const body = root.querySelector('#ai-dock-body');
  if (!body) return;
  body.innerHTML = messagesHTML();
  body.querySelectorAll('gh-md').forEach((node, index) => {
    const source = state.messages.filter((item) => item.role !== 'user')[index];
    node.textContent = source ? String(source.content || '') : '';
  });
  body.scrollTop = body.scrollHeight;
  const title = root.querySelector('#ai-dock-title');
  if (title) {
    title.textContent = state.slug ? `AI 助手 · ${state.slug}` : 'AI 助手';
  }
  const meta = root.querySelector('#ai-dock-meta');
  if (meta) {
    meta.textContent = state.busy
      ? '正在思考…'
      : state.model
        ? `模型 ${state.model}`
        : state.configured === false
          ? '未配置'
          : '就绪';
  }
  root.classList.toggle('is-busy', state.busy);
}

function setOpen(open) {
  state.open = !!open;
  if (!root) return;
  root.classList.toggle('is-open', state.open);
  root.querySelector('#ai-dock-panel').hidden = !state.open;
  root.querySelector('#ai-launcher').hidden = state.open;
  if (state.open) {
    const input = root.querySelector('#ai-dock-input textarea');
    if (input) setTimeout(() => input.focus(), 60);
    syncMessages();
    // 配置可能是在别的页面刚保存的，打开面板时重新确认一次
    if (state.configured !== true) refreshStatus();
  }
}

/* ------------------------------------------------------------ 交互 */

async function refreshStatus() {
  if (!state.slug) {
    state.configured = null;
    state.model = '';
    state.quick = [];
    syncMessages();
    return;
  }
  try {
    const data = await api.get(`/api/teams/${encodeURIComponent(state.slug)}/ai`);
    state.configured = !!data.configured;
    state.model = data.model || '';
    state.quick = Array.isArray(data.quick_prompts) ? data.quick_prompts : [];
  } catch {
    state.configured = null;
    state.quick = [];
  }
  syncMessages();
}

async function send(text) {
  const content = String(text == null ? '' : text).trim();
  if (state.busy || !content) return;
  if (!state.slug) {
    toast('请先打开一个团队页面，再向 AI 提问', 'err');
    return;
  }
  if (state.configured === false) {
    // 配置可能刚在「设置」里保存过，重新确认一次再说
    await refreshStatus();
    if (state.configured === false) {
      toast('AI 还没有配置，请到「设置 → AI 助手」填写接口信息', 'err');
      return;
    }
  }
  state.messages.push({ role: 'user', content });
  state.busy = true;
  state.error = '';
  const index = state.messages.push({ role: 'assistant', content: '', streaming: true }) - 1;
  syncMessages();

  let pending = '';
  let timer = 0;
  const flush = () => {
    timer = 0;
    state.messages[index].content = pending;
    syncMessages();
  };
  const append = (piece) => {
    pending += piece;
    if (!timer) timer = setTimeout(flush, 60);
  };

  try {
    await stream(
      `/api/teams/${encodeURIComponent(state.slug)}/ai/chat/stream`,
      { messages: state.messages.filter((item) => !item.streaming).slice(-MAX_TURNS) },
      (event, payload) => {
        if (event === 'delta') {
          append(String((payload && payload.text) || ''));
        } else if (event === 'done') {
          pending = String((payload && payload.reply) || pending);
          state.messages[index].content = pending;
          state.messages[index].streaming = false;
          if (payload && payload.model) state.model = payload.model;
        } else if (event === 'error') {
          throw new Error((payload && payload.message) || '生成中断');
        }
      },
    );
    if (!pending.trim()) {
      state.messages[index] = { role: 'assistant', content: '（模型没有返回内容）', error: true };
    }
  } catch (error) {
    const message = error && error.message ? error.message : '未知错误';
    state.messages[index] = {
      role: 'assistant',
      content: pending ? `${pending}\n\n（生成中断：${message}）` : `抱歉，这次没有拿到回复：${message}`,
      error: true,
    };
    state.error = message;
  } finally {
    if (timer) clearTimeout(timer);
    if (state.messages[index]) state.messages[index].streaming = false;
    state.busy = false;
    saveMessages();
    syncMessages();
  }
}

function clearMessages() {
  state.messages = [];
  saveMessages();
  syncMessages();
}

/* ------------------------------------------------------------ 骨架 */

function shellHTML() {
  return `
  <button class="ai-launcher" id="ai-launcher" type="button" title="AI 助手（Ctrl+J）">
    <gh-icon name="sparkle" size="16"></gh-icon><span>AI 助手</span>
  </button>
  <section class="ai-dock" id="ai-dock-panel" hidden aria-label="AI 助手">
    <header class="ai-dock__head">
      <span class="ai-dock__mark"><gh-icon name="sparkle" size="14"></gh-icon></span>
      <div class="ai-dock__titles">
        <div class="ai-dock__title" id="ai-dock-title">AI 助手</div>
        <div class="ai-dock__meta" id="ai-dock-meta">就绪</div>
      </div>
      <button class="ai-icon-btn" type="button" data-dock="expand" title="在 AI 工作台打开">
        <gh-icon name="link" size="14"></gh-icon>
      </button>
      <button class="ai-icon-btn" type="button" data-dock="clear" title="清空对话">
        <gh-icon name="trash" size="14"></gh-icon>
      </button>
      <button class="ai-icon-btn" type="button" data-dock="close" title="收起">
        <gh-icon name="chevron-down" size="14"></gh-icon>
      </button>
    </header>
    <div class="ai-dock__body" id="ai-dock-body"></div>
    <footer class="ai-dock__foot">
      <gh-textarea id="ai-dock-input" rows="2" placeholder="问点什么…（Enter 发送 / Shift+Enter 换行）"></gh-textarea>
      <div class="ai-dock__actions">
        <span class="ai-dock__tip">回答基于当前团队数据，可能有误，请核实。</span>
        <gh-btn size="sm" variant="primary" data-dock="send">发送</gh-btn>
      </div>
    </footer>
  </section>`;
}

function readInput() {
  const host = root.querySelector('#ai-dock-input');
  if (!host) return '';
  if ('value' in host && typeof host.value === 'string') return host.value;
  const inner = host.querySelector('textarea');
  return inner ? inner.value : '';
}

function clearInput() {
  const host = root.querySelector('#ai-dock-input');
  if (!host) return;
  if ('value' in host) host.value = '';
  const inner = host.querySelector('textarea');
  if (inner) inner.value = '';
}

function bind() {
  root.addEventListener('click', (event) => {
    const target = event.target;
    if (target.closest('#ai-launcher')) { setOpen(true); return; }
    const action = target.closest('[data-dock]');
    if (action) {
      const name = action.getAttribute('data-dock');
      if (name === 'close') setOpen(false);
      if (name === 'expand') {
        if (!state.slug) { toast('先打开一个团队再用 AI 工作台', 'err'); return; }
        setOpen(false);
        navigate(`/${state.slug}/ai`);
        return;
      }
      if (name === 'clear') clearMessages();
      if (name === 'send') { const text = readInput(); clearInput(); send(text); }
      return;
    }
    const chip = target.closest('[data-quick]');
    if (chip) {
      const key = chip.getAttribute('data-quick');
      const item = (state.quick || []).find((entry) => entry.key === key);
      if (key === 'project') { setOpen(false); navigate('/teams/new'); return; }
      send(item ? item.prompt : chip.textContent.trim());
    }
  });

  root.addEventListener('keydown', (event) => {
    const inside = event.target.closest && event.target.closest('#ai-dock-input');
    if (!inside) return;
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      const text = readInput();
      clearInput();
      send(text);
    }
  });

  document.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && (event.key === 'j' || event.key === 'J')) {
      event.preventDefault();
      setOpen(!state.open);
    }
  });
}

/* ------------------------------------------------------------ 对外 */

export function mountAiDock() {
  if (state.mounted) return;
  state.mounted = true;
  root = document.createElement('div');
  root.className = 'ai-root';
  root.innerHTML = shellHTML();
  document.body.appendChild(root);
  bind();
  syncMessages();
}

export function setDockContext(route) {
  if (!state.mounted) return;
  const slug = route && route.kind === 'team' ? route.slug || '' : '';
  if (slug === state.slug) return;
  saveMessages();
  state.slug = slug;
  state.configured = null;
  state.model = '';
  state.quick = [];
  loadMessages();
  syncMessages();
  refreshStatus();
}

export function openAiDock() {
  if (!state.mounted) return;
  setOpen(true);
}
