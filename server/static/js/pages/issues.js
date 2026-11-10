/* TeamFlow · 议题列表 */

import { api } from '../api.js';
import { S } from '../store.js';
import { navigate } from '../router.js';
import {
  esc, pageHead, issueRow, emptyHTML, toast, confirmAction, fieldHTML,
  onView,
} from '../ui.js';
import { openIssueDialog, loadMeta, STATUS_OPTIONS, PRIORITY_OPTIONS } from '../issue-form.js';

const STATE_OPTIONS = [
  { value: 'open', label: '未关闭' },
  { value: 'closed', label: '已关闭' },
  { value: 'all', label: '全部' },
];

const SORT_OPTIONS = [
  { value: 'updated', label: '最近更新' },
  { value: 'created', label: '最新创建' },
  { value: 'number', label: '编号' },
  { value: 'priority', label: '优先级' },
  { value: 'due', label: '截止日期' },
  { value: 'week', label: '计划周' },
];

export async function render(view, route) {
  const slug = S.detail.slug;
  const meta = await loadMeta(slug);
  const members = Array.isArray(S.detail.members) ? S.detail.members : [];

  const filters = {
    state: route.params.get('state') || 'open',
    status: route.params.get('status') || '',
    priority: route.params.get('priority') || '',
    assignee: route.params.get('assignee') || '',
    milestone: route.params.get('milestone') || '',
    label: route.params.get('label') || '',
    sort: route.params.get('sort') || 'updated',
    q: route.params.get('q') || '',
  };

  const assigneeOptions = [{ value: '', label: '全部负责人' }]
    .concat([{ value: 'none', label: '未指派' }])
    .concat(members.map((member) => ({ value: member.id, label: member.display_name || member.username })));
  const milestoneOptions = [{ value: '', label: '全部里程碑' }]
    .concat([{ value: 'none', label: '未关联里程碑' }])
    .concat(meta.milestones.map((ms) => ({ value: ms.id, label: `#${ms.number} ${ms.title}` })));
  const labelOptions = [{ value: '', label: '全部标签' }]
    .concat(meta.labels.map((label) => ({ value: label.name, label: label.name })));
  const statusOptions = [{ value: '', label: '全部状态' }].concat(STATUS_OPTIONS);
  const priorityOptions = [{ value: '', label: '全部优先级' }].concat(PRIORITY_OPTIONS);

  view.innerHTML = `${pageHead('议题', '按状态、负责人、里程碑与标签筛选团队的全部议题', `
      <gh-btn size="sm" data-act="refresh"><gh-icon name="sync" size="14"></gh-icon> 刷新</gh-btn>
      ${S.canEdit ? '<gh-btn size="sm" variant="primary" data-act="new"><gh-icon name="plus" size="14"></gh-icon> 新建议题</gh-btn>' : ''}
    `)}
    <gh-box>
      <div slot="bar" class="filter-bar">
        <gh-input name="q" icon="search" clearable placeholder="搜索标题、描述…"></gh-input>
        ${fieldHTML({ name: 'state', type: 'select', options: STATE_OPTIONS, value: filters.state })}
        ${fieldHTML({ name: 'status', type: 'select', options: statusOptions, value: filters.status })}
        ${fieldHTML({ name: 'priority', type: 'select', options: priorityOptions, value: filters.priority })}
        ${fieldHTML({ name: 'assignee', type: 'select', options: assigneeOptions, value: filters.assignee })}
        ${fieldHTML({ name: 'milestone', type: 'select', options: milestoneOptions, value: filters.milestone })}
        ${fieldHTML({ name: 'label', type: 'select', options: labelOptions, value: filters.label })}
        ${fieldHTML({ name: 'sort', type: 'select', options: SORT_OPTIONS, value: filters.sort })}
      </div>
      <div id="issue-list">
        <div style="display:flex;justify-content:center;padding:32px"><gh-spinner></gh-spinner></div>
      </div>
    </gh-box>`;

  const list = view.querySelector('#issue-list');
  const bar = view.querySelector('.filter-bar');

  const syncUrl = () => {
    const params = new URLSearchParams();
    Object.keys(filters).forEach((key) => {
      if (filters[key] && !(key === 'state' && filters[key] === 'open')) params.set(key, filters[key]);
    });
    const search = params.toString();
    const url = `/${encodeURIComponent(slug)}/issues${search ? `?${search}` : ''}`;
    history.replaceState({ tf: true }, '', url);
    window.__tfRoute = { ...route, params };
  };

  const load = async () => {
    list.innerHTML = '<div style="display:flex;justify-content:center;padding:32px"><gh-spinner></gh-spinner></div>';
    const params = {};
    Object.keys(filters).forEach((key) => {
      if (!filters[key]) return;
      if (key === 'state' && filters[key] === 'open') return;
      params[key] = filters[key];
    });
    try {
      const data = await api.get(`/api/teams/${encodeURIComponent(slug)}/issues`, params);
      const issues = Array.isArray(data.issues) ? data.issues : [];
      const counts = data.counts || {};
      if (!issues.length) {
        list.innerHTML = emptyHTML({
          icon: 'issue-opened',
          title: '没有符合条件的议题',
          desc: filters.q ? `没有与「${filters.q}」匹配的议题。` : '调整筛选条件，或创建一个新议题。',
        });
        return;
      }
      const total = counts.total ?? issues.length;
      const filtering = ['q', 'state', 'status', 'priority', 'assignee', 'milestone', 'label'].some((key) => filters[key]);
      list.innerHTML = `<div class="list-summary small muted">共 ${total} 个议题 · 未关闭 ${counts.open ?? 0} · 已关闭 ${counts.closed ?? 0}${filtering ? ` · 当前筛选 ${issues.length} 条` : ''}</div>
        ${issues.map((issue) => issueRow(issue, { slug, labelLimit: 4 })).join('')}`;
    } catch (error) {
      list.innerHTML = `<div class="error-box"><gh-icon name="alert" size="24"></gh-icon>
        <div class="error-box__title">议题加载失败</div>
        <div class="error-box__msg">${esc(error.message || '')}</div>
        <gh-btn size="sm" data-act="retry">重试</gh-btn></div>`;
    }
  };

  if (bar) {
    const search = bar.querySelector('[name="q"]');
    if (search) search.value = filters.q;
    bar.addEventListener('change', (event) => {
      const field = event.target.closest('[name]');
      if (!field) return;
      const name = field.getAttribute('name');
      if (!(name in filters)) return;
      filters[name] = field.value;
      syncUrl();
      load();
    });
    bar.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter') return;
      const field = event.target.closest('[name="q"]');
      if (!field) return;
      event.preventDefault();
      filters.q = String(field.value || '').trim();
      syncUrl();
      load();
    });
    bar.addEventListener('click', (event) => {
      if (event.target.closest('[data-act="clear"]')) {
        Object.keys(filters).forEach((key) => { filters[key] = key === 'state' ? 'open' : (key === 'sort' ? 'updated' : ''); });
        render(view, route);
      }
    });
  }

  onView(view, 'click', async (event) => {
    const act = event.target.closest('[data-act]');
    if (act) {
      const name = act.getAttribute('data-act');
      if (name === 'new') {
        await openIssueDialog({ slug, onSaved: (issue) => { load(); navigate(`/${slug}/issues/${issue.number}`); } });
        return;
      }
      if (name === 'refresh') { load(); return; }
      if (name === 'retry') { load(); return; }
      return;
    }
    const row = event.target.closest('[data-number]');
    if (row) navigate(`/${slug}/issues/${row.getAttribute('data-number')}`);
  });

  onView(view, 'keydown', (event) => {
    if (event.key === 'n' && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      if (S.canEdit) openIssueDialog({ slug, onSaved: (issue) => { load(); navigate(`/${slug}/issues/${issue.number}`); } });
    }
  });

  await load();

  if (route.params.get('new') && S.canEdit) {
    openIssueDialog({ slug, onSaved: (issue) => { load(); navigate(`/${slug}/issues/${issue.number}`); } });
  }
}
