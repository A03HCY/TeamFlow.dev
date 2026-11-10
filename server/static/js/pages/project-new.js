/* TeamFlow · 用 AI 新建项目组（/teams/new） */

import { api } from '../api.js';
import { S, bootstrap, invalidateTeam } from '../store.js';
import { esc, toast, emptyHTML, SPINNER } from '../ui.js';
import { navigate } from '../router.js';

const EXAMPLE = '做一个面向设计师的配色协作平台：团队可以共享配色方案、导出 CSS 变量，并在评审时对比版本。';

const state = {
  draft: null,
  busy: false,
  goal: '',
  name: '',
};

function inputValue(el) {
  if (!el) return '';
  const inner = el.querySelector('input, textarea, select');
  if (inner) return String(inner.value || '');
  return String(el.value || '');
}

function shortDate(value) {
  return value ? String(value).slice(0, 10) : '';
}

/* ------------------------------------------------------------ 草稿视图 */

function milestoneHTML(item, index, total) {
  return `<div class="pn-item" data-ms="${index}">
    <div class="pn-item__head">
      <span class="pn-item__badge">M${index + 1}</span>
      <gh-input name="ms-title-${index}" placeholder="里程碑标题" value="${esc(item.title || '')}"></gh-input>
      <button class="ai-icon-btn" type="button" data-act="remove-ms" data-index="${index}" title="移除">
        <gh-icon name="x" size="13"></gh-icon>
      </button>
    </div>
    <gh-textarea name="ms-acc-${index}" rows="3" placeholder="- [ ] 可验收的结果"
      value="${esc(item.acceptance || '')}"></gh-textarea>
    <div class="row pn-item__dates">
      <gh-input name="ms-start-${index}" type="date" value="${esc(shortDate(item.start_date))}"></gh-input>
      <span class="muted small">→</span>
      <gh-input name="ms-due-${index}" type="date" value="${esc(shortDate(item.due_date))}"></gh-input>
      ${item.kind === 'continuous' ? '<gh-badge state="accent">持续</gh-badge>' : ''}
      <span class="grow"></span>
      <span class="small muted">第 ${index + 1} / ${total} 个</span>
    </div>
  </div>`;
}

function issueHTML(item, index, milestones) {
  const options = ['<option value="0">未归属</option>'].concat(
    milestones.map((ms, i) => `<option value="${i + 1}"${item.milestone === i + 1 ? ' selected' : ''}>M${i + 1} ${esc(ms.title || '')}</option>`)
  ).join('');
  return `<div class="pn-item pn-item--issue" data-issue="${index}">
    <div class="pn-item__head">
      <gh-icon name="issue-opened" size="14"></gh-icon>
      <gh-input name="is-title-${index}" placeholder="议题标题" value="${esc(item.title || '')}"></gh-input>
      <button class="ai-icon-btn" type="button" data-act="remove-issue" data-index="${index}" title="移除">
        <gh-icon name="x" size="13"></gh-icon>
      </button>
    </div>
    <div class="row pn-item__meta">
      <gh-select name="is-priority-${index}" size="sm">
        <option value="high"${item.priority === 'high' ? ' selected' : ''}>高优先级</option>
        <option value="medium"${item.priority === 'medium' ? ' selected' : ''}>中优先级</option>
        <option value="low"${item.priority === 'low' ? ' selected' : ''}>低优先级</option>
      </gh-select>
      <gh-select name="is-milestone-${index}" size="sm">${options}</gh-select>
      <gh-input name="is-hours-${index}" type="number" icon="clock" placeholder="工时"
        value="${item.estimate_hours == null ? '' : item.estimate_hours}"></gh-input>
      ${(item.labels || []).map((label) => `<gh-label color="#6b7280">${esc(label)}</gh-label>`).join('')}
    </div>
  </div>`;
}

function draftHTML(draft) {
  const milestones = draft.milestones || [];
  const issues = draft.issues || [];
  const warnings = draft.warnings || [];
  return `<div class="pn-draft">
    <div class="pn-name">
      <gh-input name="draft-name" placeholder="项目组名称" value="${esc(draft.name || '')}"></gh-input>
      <gh-input name="draft-desc" placeholder="一句话项目目标" value="${esc(draft.description || '')}"></gh-input>
    </div>
    ${warnings.length ? `<gh-alert type="warning" dismissible>${esc(warnings.join('；'))}</gh-alert>` : ''}
    <div class="pn-section__head">
      <span>里程碑 ${milestones.length}</span>
      <button class="ai-icon-btn" type="button" data-act="add-ms" title="添加里程碑">
        <gh-icon name="plus" size="13"></gh-icon>
      </button>
    </div>
    <div class="pn-list">${milestones.map((item, index) => milestoneHTML(item, index, milestones.length)).join('')}</div>
    <div class="pn-section__head">
      <span>议题 ${issues.length}</span>
      <button class="ai-icon-btn" type="button" data-act="add-issue" title="添加议题">
        <gh-icon name="plus" size="13"></gh-icon>
      </button>
    </div>
    <div class="pn-list">${issues.map((item, index) => issueHTML(item, index, milestones)).join('')}</div>
    <div class="pn-submit">
      <span class="small muted">创建后可以在团队里继续调整；议题编号从 #1 开始。</span>
      <gh-btn variant="primary" data-act="create"${S.requireLogin ? '' : ' disabled'}>创建项目组</gh-btn>
    </div>
  </div>`;
}

function collectDraft(host) {
  const draft = {
    name: inputValue(host.querySelector('[name="draft-name"]')).trim(),
    description: inputValue(host.querySelector('[name="draft-desc"]')).trim(),
    slug: (state.draft && state.draft.slug) || '',
    milestones: [],
    issues: [],
    warnings: [],
  };
  host.querySelectorAll('[data-ms]').forEach((block, index) => {
    draft.milestones.push({
      title: inputValue(block.querySelector(`[name="ms-title-${index}"]`)).trim(),
      acceptance: inputValue(block.querySelector(`[name="ms-acc-${index}"]`)),
      kind: 'scheduled',
      start_date: inputValue(block.querySelector(`[name="ms-start-${index}"]`)).trim() || null,
      due_date: inputValue(block.querySelector(`[name="ms-due-${index}"]`)).trim() || null,
    });
  });
  host.querySelectorAll('[data-issue]').forEach((block, index) => {
    const hours = Number(inputValue(block.querySelector(`[name="is-hours-${index}"]`)));
    draft.issues.push({
      title: inputValue(block.querySelector(`[name="is-title-${index}"]`)).trim(),
      body: '',
      priority: inputValue(block.querySelector(`[name="is-priority-${index}"]`)) || 'medium',
      estimate_hours: Number.isFinite(hours) && hours > 0 ? hours : null,
      milestone: Number(inputValue(block.querySelector(`[name="is-milestone-${index}"]`))) || 0,
      labels: [],
    });
  });
  return draft;
}

/* ------------------------------------------------------------ 动作 */

async function generate(view) {
  if (state.busy) return;
  const goal = inputValue(view.querySelector('[name="goal"]')).trim();
  const name = inputValue(view.querySelector('[name="name"]')).trim();
  if (goal.length < 4) { toast('请先用一句话描述这个项目要做什么', 'err'); return; }
  state.goal = goal;
  state.name = name;
  state.busy = true;
  const box = view.querySelector('#pn-draft');
  const btn = view.querySelector('[data-act="generate"]');
  if (btn) btn.disabled = true;
  if (box) box.innerHTML = `<div class="pn-loading">${SPINNER}<span class="small muted">AI 正在拆解里程碑与议题，通常需要 10–30 秒…</span></div>`;
  try {
    const data = await api.post('/api/ai/project/draft', { goal, name });
    state.draft = data.draft;
    if (box) box.innerHTML = draftHTML(data.draft);
    toast('初稿已生成，确认无误后创建');
  } catch (error) {
    if (box) {
      box.innerHTML = emptyHTML({
        icon: 'alert',
        title: '生成失败',
        desc: (error && error.message) || 'AI 没有返回可用的草稿',
      });
    }
    toast((error && error.message) || '生成失败', 'err');
  } finally {
    state.busy = false;
    if (btn) btn.disabled = false;
  }
}

async function create(view) {
  if (state.busy) return;
  if (!S.requireLogin) { toast('请先在设置里开启登录模式，再创建项目组', 'err'); return; }
  const host = view.querySelector('#pn-draft');
  if (!host || !host.querySelector('[data-ms]')) { toast('请先生成草稿', 'err'); return; }
  const draft = collectDraft(host);
  if (!draft.milestones.length && !draft.issues.length) { toast('草稿里没有里程碑或议题', 'err'); return; }
  state.busy = true;
  const btn = host.querySelector('[data-act="create"]');
  if (btn) btn.disabled = true;
  try {
    const data = await api.post('/api/ai/project/create', { draft });
    await bootstrap();
    invalidateTeam();
    toast(`项目组「${data.team.name}」已创建：${data.milestones.length} 个里程碑、${data.issues.length} 个议题`);
    navigate(`/${data.team.slug}/overview`);
  } catch (error) {
    toast((error && error.message) || '创建失败', 'err');
    if (btn) btn.disabled = false;
  } finally {
    state.busy = false;
  }
}

function addMilestone(host) {
  const draft = collectDraft(host);
  draft.milestones.push({ title: '', acceptance: '', kind: 'scheduled', start_date: null, due_date: null });
  state.draft = draft;
  host.innerHTML = draftHTML(draft);
}

function addIssue(host) {
  const draft = collectDraft(host);
  draft.issues.push({ title: '', priority: 'medium', estimate_hours: null, milestone: 0, labels: [] });
  state.draft = draft;
  host.innerHTML = draftHTML(draft);
}

function removeAt(host, list, index) {
  const draft = collectDraft(host);
  draft[list].splice(index, 1);
  state.draft = draft;
  host.innerHTML = draftHTML(draft);
}

/* ------------------------------------------------------------ 页面 */

export async function render(view, route) {
  document.title = '用 AI 新建项目组 · TeamFlow';
  state.draft = null;
  const loginHint = S.requireLogin
    ? ''
    : `<gh-alert type="warning">当前是免登录模式（全平台只有一个团队），可以生成并查看草稿，但需要先在「设置」里开启登录模式才能真正创建项目组。</gh-alert>`;

  view.innerHTML = `
  <div class="center-wrap center-wrap--top">
    <div class="pn-page">
      <div class="teams-head">
        <div class="col" style="gap:4px">
          <a class="small muted row" href="/teams" style="gap:2px">
            <gh-icon name="chevron-left" size="14"></gh-icon> 全部团队
          </a>
          <h2 class="teams-head__title">用 AI 新建项目组</h2>
          <p class="small muted">描述目标 → AI 给出里程碑与议题初稿 → 你确认后一次性创建。</p>
        </div>
      </div>
      ${loginHint}
      <div class="pn-grid">
        <gh-box title="1 · 描述项目">
          <div class="pn-form">
            <gh-input name="name" icon="organization" placeholder="项目组名称（可留空，让 AI 起名）"></gh-input>
            <gh-textarea name="goal" rows="6" placeholder="${esc(EXAMPLE)}"></gh-textarea>
            <div class="row">
              <gh-btn variant="primary" data-act="generate">让 AI 生成初稿</gh-btn>
              <a class="small muted" href="${(S.teams && S.teams[0]) ? `/${esc(S.teams[0].slug)}/settings` : '/login'}">AI 配置</a>
            </div>
            <p class="small muted">初稿只用于确认，不会直接落库；创建前可以改标题、验收标准、负责人和工时。</p>
          </div>
        </gh-box>
        <gh-box title="2 · 确认草稿">
          <div id="pn-draft">
            ${emptyHTML({ icon: 'sparkle', title: '还没有草稿', desc: '在左边写下项目目标，让 AI 先给一版里程碑与议题。' })}
          </div>
        </gh-box>
      </div>
    </div>
  </div>`;

  view.addEventListener('click', (event) => {
    const act = event.target.closest('[data-act]');
    if (!act) return;
    const name = act.getAttribute('data-act');
    const host = view.querySelector('#pn-draft');
    if (name === 'generate') { generate(view); return; }
    if (!host) return;
    if (name === 'create') { create(view); return; }
    if (name === 'add-ms') { addMilestone(host); return; }
    if (name === 'add-issue') { addIssue(host); return; }
    if (name === 'remove-ms') { removeAt(host, 'milestones', Number(act.getAttribute('data-index'))); return; }
    if (name === 'remove-issue') { removeAt(host, 'issues', Number(act.getAttribute('data-index'))); }
  });

  view.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      event.preventDefault();
      generate(view);
    }
  });

  if (state.goal) {
    const goal = view.querySelector('[name="goal"]');
    const name = view.querySelector('[name="name"]');
    if (goal) goal.value = state.goal;
    if (name) name.value = state.name;
  }
}
