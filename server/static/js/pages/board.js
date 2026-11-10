/* TeamFlow · 看板 */

import { api } from '../api.js';
import { S } from '../store.js';
import { navigate } from '../router.js';
import {
  esc, pageHead, avatarHTML, labelChips, priorityBadge, fmtDate, isOverdue, emptyHTML, toast,
  onView,
} from '../ui.js';
import { openIssueDialog } from '../issue-form.js';

const COLUMNS = [
  { status: 'todo', title: '待办', icon: 'issue-opened' },
  { status: 'in_progress', title: '进行中', icon: 'play' },
  { status: 'done', title: '已完成', icon: 'check-circle' },
];

function cardHTML(issue, slug) {
  const assignee = issue.assignee_id
    ? { display_name: issue.assignee_name, avatar_url: issue.assignee_avatar_url }
    : null;
  return `<div class="board-card" draggable="${S.canEdit ? 'true' : 'false'}" data-number="${esc(issue.number)}">
    <a class="board-card__title" href="/${esc(slug)}/issues/${esc(issue.number)}">${esc(issue.title)}</a>
    <div class="row small muted" style="gap:6px">
      <span class="mono">#${esc(issue.number)}</span>
      ${issue.due_date ? `<span class="row-tight${isOverdue(issue.due_date) && issue.state !== 'closed' ? ' is-late' : ''}"><gh-icon name="clock" size="12"></gh-icon>${esc(fmtDate(issue.due_date))}</span>` : ''}
      <span class="grow"></span>
      ${priorityBadge(issue.priority, true)}
      ${assignee ? avatarHTML(assignee, 18) : ''}
    </div>
    ${issue.labels && issue.labels.length ? `<div class="row" style="gap:4px">${labelChips(issue.labels, 3)}</div>` : ''}
  </div>`;
}

export async function render(view, route) {
  const slug = S.detail.slug;
  const members = Array.isArray(S.detail.members) ? S.detail.members : [];
  const assignee = route.params.get('assignee') || '';

  const memberOptions = [{ value: '', label: '全部成员' }]
    .concat(members.map((member) => ({ value: member.id, label: member.display_name || member.username })));
  const optionTags = memberOptions.map((option) => `<option value="${esc(option.value)}"${String(assignee) === String(option.value) ? ' selected' : ''}>${esc(option.label)}</option>`).join('');

  view.innerHTML = `${pageHead('看板', '拖拽卡片即可调整议题状态', `
      <gh-btn size="sm" data-act="refresh"><gh-icon name="sync" size="14"></gh-icon> 刷新</gh-btn>
      ${S.canEdit ? '<gh-btn size="sm" variant="primary" data-act="new"><gh-icon name="plus" size="14"></gh-icon> 新建议题</gh-btn>' : ''}
    `)}
    <gh-box>
      <div slot="bar" class="row" style="gap:8px">
        <gh-field><gh-select name="assignee">${optionTags}</gh-select></gh-field>
        <span class="small muted">共 <b id="board-total">0</b> 个议题${S.canEdit ? ' · 拖动卡片切换状态' : ''}</span>
      </div>
      <div id="board" class="board">
        <div style="display:flex;justify-content:center;padding:32px;width:100%"><gh-spinner></gh-spinner></div>
      </div>
    </gh-box>`;

  const board = view.querySelector('#board');
  const total = view.querySelector('#board-total');
  let issues = [];
  let dragging = null;

  const groups = () => {
    const map = { todo: [], in_progress: [], done: [] };
    issues.forEach((issue) => {
      const key = map[issue.status] ? issue.status : 'todo';
      map[key].push(issue);
    });
    return map;
  };

  const paint = () => {
    const map = groups();
    board.innerHTML = COLUMNS.map((column) => {
      const items = map[column.status] || [];
      return `<div class="board-col" data-status="${column.status}">
        <div class="board-col__head">
          <gh-icon name="${column.icon}" size="14"></gh-icon>
          <span>${esc(column.title)}</span>
          <span class="grow"></span>
          <span class="muted small">${items.length}</span>
        </div>
        <div class="board-col__body">
          ${items.length ? items.map((issue) => cardHTML(issue, slug)).join('') : `<div class="board-col__empty small muted">暂无议题</div>`}
        </div>
      </div>`;
    }).join('');
    if (total) total.textContent = String(issues.length);
  };

  const load = async () => {
    const params = { state: 'all', sort: 'updated' };
    if (assignee) params.assignee = assignee;
    try {
      const data = await api.get(`/api/teams/${encodeURIComponent(slug)}/issues`, params);
      issues = Array.isArray(data.issues) ? data.issues : [];
      paint();
    } catch (error) {
      board.innerHTML = `<div class="error-box"><gh-icon name="alert" size="24"></gh-icon>
        <div class="error-box__title">看板加载失败</div>
        <div class="error-box__msg">${esc(error.message || '')}</div>
        <gh-btn size="sm" data-act="retry">重试</gh-btn></div>`;
    }
  };

  const bar = view.querySelector('gh-select[name="assignee"]');
  if (bar) {
    bar.addEventListener('change', () => {
      const value = bar.value || '';
      const url = value ? `/${encodeURIComponent(slug)}/board?assignee=${encodeURIComponent(value)}` : `/${encodeURIComponent(slug)}/board`;
      history.replaceState({ tf: true }, '', url);
      window.__tfRoute = { ...route, params: new URLSearchParams(value ? `assignee=${encodeURIComponent(value)}` : '') };
      load();
    });
  }

  board.addEventListener('dragstart', (event) => {
    const card = event.target.closest('.board-card');
    if (!card || !S.canEdit) return;
    dragging = card.getAttribute('data-number');
    card.classList.add('is-drag');
    event.dataTransfer.setData('text/plain', dragging);
    event.dataTransfer.effectAllowed = 'move';
  });

  board.addEventListener('dragend', () => {
    dragging = null;
    board.querySelectorAll('.is-drag').forEach((node) => node.classList.remove('is-drag'));
    board.querySelectorAll('.is-over').forEach((node) => node.classList.remove('is-over'));
  });

  board.addEventListener('dragover', (event) => {
    if (!S.canEdit) return;
    const column = event.target.closest('.board-col');
    if (!column) return;
    event.preventDefault();
    board.querySelectorAll('.board-col.is-over').forEach((node) => { if (node !== column) node.classList.remove('is-over'); });
    column.classList.add('is-over');
  });

  board.addEventListener('dragleave', (event) => {
    const column = event.target.closest('.board-col');
    if (column && !column.contains(event.relatedTarget)) column.classList.remove('is-over');
  });

  board.addEventListener('drop', async (event) => {
    if (!S.canEdit) return;
    const column = event.target.closest('.board-col');
    if (!column) return;
    event.preventDefault();
    const status = column.getAttribute('data-status');
    const number = dragging || event.dataTransfer.getData('text/plain');
    dragging = null;
    column.classList.remove('is-over');
    if (!number || !status) return;
    const issue = issues.find((item) => String(item.number) === String(number));
    if (!issue || issue.status === status) return;
    const previous = issue.status;
    issue.status = status;
    paint();
    try {
      await api.patch(`/api/teams/${encodeURIComponent(slug)}/issues/${number}`, { status });
      toast(`#${number} 已移动到${COLUMNS.find((c) => c.status === status).title}`);
      await load();
    } catch (error) {
      issue.status = previous;
      paint();
      toast(error.message || '状态更新失败', 'err');
    }
  });

  onView(view, 'click', async (event) => {
    const act = event.target.closest('[data-act]');
    if (act) {
      const name = act.getAttribute('data-act');
      if (name === 'new') {
        await openIssueDialog({ slug, onSaved: () => load() });
        return;
      }
      if (name === 'refresh' || name === 'retry') { load(); return; }
      return;
    }
    const card = event.target.closest('.board-card');
    if (card && !event.target.closest('a')) navigate(`/${slug}/issues/${card.getAttribute('data-number')}`);
  });

  await load();
}
