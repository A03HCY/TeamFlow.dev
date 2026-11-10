/* TeamFlow · 周计划 */

import { api } from '../api.js';
import { S } from '../store.js';
import { navigate } from '../router.js';
import {
  esc, pageHead, issueRow, avatarHTML, emptyHTML, toast, confirmAction, fmtWeek, fmtDate,
  statusBadge,
  onView,
} from '../ui.js';

function rolloverList(items) {
  if (!Array.isArray(items) || !items.length) return '';
  const rows = items.map((item) => `<li><span class="mono">#${esc(item.number)}</span> ${esc(item.title)} ${statusBadge(item.status, false)}${item.assignee_name ? `<span class="muted small"> · ${esc(item.assignee_name)}</span>` : ''}</li>`).join('');
  return `<ul class="dlg-list">${rows}</ul>`;
}

export async function render(view, route) {
  const slug = S.detail.slug;
  const token = route.arg || route.params.get('week') || 'current';

  const go = (next) => {
    const target = !next || next === 'current' ? `/${slug}/week` : `/${slug}/week/${next}`;
    navigate(target);
  };

  let data;
  try {
    data = await api.get(`/api/teams/${encodeURIComponent(slug)}/week`, { week: token });
  } catch (error) {
    view.innerHTML = `<div class="error-box"><gh-icon name="alert" size="24"></gh-icon>
      <div class="error-box__title">周计划加载失败</div>
      <div class="error-box__msg">${esc(error.message || '')}</div>
      <gh-btn size="sm" data-act="retry">重试</gh-btn></div>`;
    onView(view, 'click', (event) => {
      if (event.target.closest('[data-act="retry"]')) render(view, route);
    });
    return;
  }

  const week = data.week || '';
  const groups = Array.isArray(data.groups) ? data.groups : [];
  const unassigned = Array.isArray(data.unassigned) ? data.unassigned : [];
  const unscheduled = Array.isArray(data.unscheduled) ? data.unscheduled : [];
  const stats = data.stats || {};
  const rollover = data.rollover || { count: 0, items: [] };
  const isCurrent = String(data.current) === String(week);

  const groupHTML = groups.map((group) => `
    <div class="week-group">
      <div class="week-group__head">
        ${avatarHTML({ display_name: group.name, avatar_url: group.avatar_url }, 22)}
        <b>${esc(group.name || group.username || '成员')}</b>
        <span class="muted small">${group.items ? group.items.length : 0} 项</span>
        <span class="grow"></span>
      </div>
      ${group.items && group.items.length
        ? group.items.map((issue) => issueRow(issue, { slug, labelLimit: 3 })).join('')
        : '<div class="week-group__empty small muted">本周还没有安排议题</div>'}
    </div>`).join('');

  const unscheduledHTML = unscheduled.length ? unscheduled.map((issue) => `
    <div class="week-pick" data-number="${esc(issue.number)}">
      <gh-icon name="${issue.state === 'closed' ? 'issue-closed' : 'issue-opened'}" size="16"></gh-icon>
      <a class="grow" href="/${esc(slug)}/issues/${esc(issue.number)}">${esc(issue.title)}</a>
      <span class="mono small muted">#${esc(issue.number)}</span>
      ${issue.assignee_name ? `<span class="small muted">${esc(issue.assignee_name)}</span>` : '<span class="small muted">未指派</span>'}
      ${S.canEdit ? `<gh-btn size="sm" variant="invisible" data-act="plan" data-number="${esc(issue.number)}">排入本周</gh-btn>` : ''}
    </div>`).join('') : emptyHTML({ icon: 'check', title: '没有待排期的议题', desc: '未排期的开放议题会出现在这里。' });

  view.innerHTML = `${pageHead('周计划', `${esc(data.label || fmtWeek(week))} · ${esc(data.range || '')}`, `
      <gh-btn size="sm" data-act="prev"><gh-icon name="chevron-left" size="14"></gh-icon> 上一周</gh-btn>
      <gh-btn size="sm"${isCurrent ? ' disabled' : ''} data-act="today">本周</gh-btn>
      <gh-btn size="sm" data-act="next">下一周 <gh-icon name="chevron-right" size="14"></gh-icon></gh-btn>
      ${S.canEdit && rollover.count ? '<gh-btn size="sm" variant="primary" data-act="rollover">顺延未完成</gh-btn>' : ''}
    `)}

    <div class="grid-stats">
      <gh-stat label="本周计划" value="${stats.planned ?? 0}" icon="calendar" tone="accent"></gh-stat>
      <gh-stat label="未完成" value="${stats.open ?? 0}" icon="issue-opened" tone="warn"></gh-stat>
      <gh-stat label="已完成" value="${stats.done ?? 0}" icon="check-circle" tone="ok"></gh-stat>
      <gh-stat label="待排期" value="${stats.unscheduled ?? 0}" icon="inbox"></gh-stat>
    </div>

    ${rollover.count ? `<gh-alert type="note">上一周还有 <b>${rollover.count}</b> 个未完成议题${S.canEdit ? '，可以顺延到本周' : ''}。</gh-alert>` : ''}

    <div class="week-groups">
      ${groupHTML || emptyHTML({ icon: 'people', title: '还没有成员', desc: '邀请成员后即可按人查看每周计划。' })}
      ${unassigned.length ? `<div class="week-group">
        <div class="week-group__head"><gh-icon name="person" size="16"></gh-icon><b>未指派</b><span class="muted small">${unassigned.length} 项</span></div>
        ${unassigned.map((issue) => issueRow(issue, { slug, labelLimit: 3 })).join('')}
      </div>` : ''}
      ${!groupHTML && !unassigned.length ? `<div class="week-groups__empty">${emptyHTML({ icon: 'calendar', title: '本周还没有安排', desc: '把未排期的议题排入本周试试。' })}</div>` : ''}
    </div>

    <gh-box title="待排期议题">
      <div class="week-picks">${unscheduledHTML}</div>
    </gh-box>`;

  onView(view, 'click', async (event) => {
    const act = event.target.closest('[data-act]');
    if (act) {
      const name = act.getAttribute('data-act');
      try {
        if (name === 'prev') { go(data.prev); return; }
        if (name === 'next') { go(data.next); return; }
        if (name === 'today') { go('current'); return; }
        if (name === 'retry') { render(view, route); return; }
        if (name === 'plan') {
          const number = act.getAttribute('data-number');
          await api.patch(`/api/teams/${encodeURIComponent(slug)}/issues/${number}`, { plan_week: week });
          toast(`#${number} 已排入 ${fmtWeek(week)}`);
          render(view, route);
          return;
        }
        if (name === 'rollover') {
          const ok = await confirmAction({
            title: '顺延未完成的议题',
            message: `将上一周的 ${rollover.count} 个未完成议题顺延到 ${fmtWeek(week)}？`,
            okText: '顺延',
          });
          if (!ok) return;
          const result = await api.post(`/api/teams/${encodeURIComponent(slug)}/week/rollover`, {
            from: rollover.from,
            to: week,
          });
          toast(`已顺延 ${result.count || 0} 个议题`);
          render(view, route);
          return;
        }
      } catch (error) {
        toast(error.message || '操作失败', 'err');
      }
      return;
    }
    const row = event.target.closest('[data-number]');
    if (row && !event.target.closest('a') && !event.target.closest('gh-btn')) {
      navigate(`/${slug}/issues/${row.getAttribute('data-number')}`);
    }
  });
}
