/* TeamFlow · 概览 */

import { api } from '../api.js';
import { S, invalidateTeam } from '../store.js';
import { navigate } from '../router.js';
import {
  esc, pageHead, avatarHTML, statusBadge, timeAgo, fmtDate, fmtWeek,
  emptyHTML, toast,
  onView,
} from '../ui.js';

function statCard(label, value, options = {}) {
  const unit = options.unit || '';
  const tone = options.tone ? ` tone="${options.tone}"` : '';
  const icon = options.icon ? ` icon="${options.icon}"` : '';
  const foot = options.foot || '';
  const footAttr = foot ? ` foot="${esc(foot)}"` : '';
  const unitAttr = unit ? ` unit="${esc(unit)}"` : '';
  return `<gh-stat label="${esc(label)}" value="${esc(value)}"${unitAttr}${tone}${icon}${footAttr}></gh-stat>`;
}

function milestoneRow(milestone, slug) {
  const progress = Number(milestone.progress || 0);
  const dates = milestone.kind === 'continuous'
    ? '持续进行'
    : `${fmtDate(milestone.start_date)} → ${fmtDate(milestone.due_date)}`;
  return `<div class="row" data-ms="${esc(milestone.number)}" style="align-items:flex-start;padding:10px 0;border-bottom:1px solid var(--gh-border-muted);cursor:pointer">
    <gh-icon name="milestone" size="16"></gh-icon>
    <div class="grow">
      <div class="row">
        <b>${esc(milestone.title)}</b>
        <span class="grow"></span>
        <span class="small muted">${esc(milestone.closed_count ?? milestone.closed ?? 0)}/${esc(milestone.total ?? 0)} · ${Math.round(progress)}%</span>
      </div>
      <div class="ms-progress"><gh-progress value="${progress}" thin tone="${progress >= 100 ? 'ok' : 'accent'}"></gh-progress></div>
      <div class="small muted">${esc(dates)}${milestone.state === 'closed' ? ' · 已完成' : ''}</div>
    </div>
  </div>`;
}

function activityRow(item) {
  const verbMap = {
    created: '创建了', updated: '更新了', deleted: '删除了',
    commented: '评论了', closed: '关闭了', reopened: '重新打开了',
  };
  const verb = verbMap[item.verb] || item.verb || '操作了';
  const typeMap = { issue: '议题', milestone: '里程碑', schedule: '日程', member: '成员', label: '标签', setting: '设置', week: '周计划' };
  const type = typeMap[item.subject_type] || '';
  const subject = item.subject_text || (item.subject_id ? `#${item.subject_id}` : '');
  return `<div class="activity__row">
    ${avatarHTML({ display_name: item.actor_name || '?' }, 22)}
    <div class="activity__text">
      <b>${esc(item.actor_name || '访客')}</b> ${esc(verb)}${type ? esc(type) : ''}${subject ? ` ${esc(subject)}` : ''}
      ${item.detail ? `<div class="small muted">${esc(item.detail)}</div>` : ''}
    </div>
    <span class="activity__time small muted">${esc(timeAgo(item.created_at))}</span>
  </div>`;
}

function trendChart(trend) {
  const rows = Array.isArray(trend) ? trend : [];
  const max = Math.max(1, ...rows.map((d) => Math.max(Number(d.created || 0), Number(d.closed || 0))));
  const sumCreated = rows.reduce((total, day) => total + Number(day.created || 0), 0);
  const sumClosed = rows.reduce((total, day) => total + Number(day.closed || 0), 0);
  const cols = rows.map((day, index) => {
    const created = Number(day.created || 0);
    const closed = Number(day.closed || 0);
    const hCreated = Math.round((created / max) * 100);
    const hClosed = Math.round((closed / max) * 100);
    const showLabel = index % 2 === 0 || index === rows.length - 1;
    return `<div class="trend__col" title="${esc(day.day)} 新建 ${created} / 完成 ${closed}">
      <div class="trend__bars">
        <div class="trend__bar" style="height:${hCreated}%"></div>
        <div class="trend__bar is-closed" style="height:${hClosed}%"></div>
      </div>
      <div class="trend__label">${showLabel ? esc(String(day.day || '').slice(5)) : ''}</div>
    </div>`;
  }).join('');
  return `<div class="trend-legend">
      <span class="row-tight"><span class="legend-dot"></span>新建 ${sumCreated}</span>
      <span class="row-tight"><span class="legend-dot is-closed"></span>完成 ${sumClosed}</span>
    </div>
    <div class="trend">${cols}</div>`;
}

function workloadTable(rows) {
  const list = Array.isArray(rows) ? rows : [];
  if (!list.length) return emptyHTML({ icon: 'people', title: '还没有成员' });
  const body = list.map((member) => `<tr>
    <td><div class="row-tight">${avatarHTML(member, 20)}<span>${esc(member.display_name || member.username || '成员')}</span></div></td>
    <td class="mono">${Number(member.open_count || 0)}</td>
    <td class="mono">${Number(member.closed_count || 0)}</td>
  </tr>`).join('');
  return `<gh-table dense>
    <table>
      <thead><tr><th>成员</th><th>进行中</th><th>已完成</th></tr></thead>
      <tbody>${body}</tbody>
    </table>
  </gh-table>`;
}

export async function render(view, route) {
  const slug = S.detail.slug;
  let data;
  try {
    data = await api.get(`/api/teams/${slug}/overview`);
  } catch (error) {
    view.innerHTML = `<div class="error-box"><gh-icon name="alert" size="24"></gh-icon>
      <div class="error-box__title">概览加载失败</div>
      <div class="error-box__msg">${esc(error.message || '')}</div>
      <gh-btn size="sm" data-act="retry">重试</gh-btn></div>`;
    onView(view, 'click', (event) => {
      if (event.target.closest('[data-act="retry"]')) render(view, route);
    });
    return;
  }

  const stats = data.stats || {};
  const team = data.team || S.detail || {};
  const milestones = Array.isArray(data.milestones) ? data.milestones : [];
  const activity = Array.isArray(data.activity) ? data.activity : [];
  const workload = Array.isArray(data.workload) ? data.workload : [];
  const trend = Array.isArray(data.trend) ? data.trend : [];

  const description = team.description || '团队的议题、里程碑与日程总览';

  view.innerHTML = `${pageHead('概览', description, `
      <gh-btn size="sm" data-act="refresh"><gh-icon name="sync" size="14"></gh-icon> 刷新</gh-btn>
      ${S.canEdit ? '<gh-btn size="sm" variant="primary" data-act="new-issue"><gh-icon name="plus" size="14"></gh-icon> 新建议题</gh-btn>' : ''}
    `)}

    <div class="grid-stats">
      ${statCard('未关闭议题', stats.open ?? 0, { icon: 'issue-opened', tone: 'accent', foot: `共 ${stats.total ?? 0} 个议题` })}
      ${statCard('进行中', stats.in_progress ?? 0, { icon: 'play', tone: 'warn', foot: `待办 ${stats.todo ?? 0} 个` })}
      ${statCard('已完成', stats.done ?? 0, { icon: 'check-circle', tone: 'ok', foot: `已关闭 ${stats.closed ?? 0} 个` })}
      ${statCard('完成率', Math.round(Number(stats.completion || 0)), { unit: '%', icon: 'rocket', tone: 'purple', foot: `逾期 ${stats.overdue ?? 0} · 高优 ${stats.high_open ?? 0}` })}
    </div>

    <div class="split-2">
      <gh-box title="里程碑进度">
        ${milestones.length ? milestones.slice(0, 6).map((ms) => milestoneRow(ms, slug)).join('') : emptyHTML({ icon: 'milestone', title: '暂无里程碑', desc: '在里程碑页创建第一个里程碑。' })}
        ${milestones.length > 6 ? `<div class="small muted" style="padding-top:8px">还有 ${milestones.length - 6} 个里程碑</div>` : ''}
      </gh-box>
      <gh-box title="最近动态">
        ${activity.length ? `<div class="activity">${activity.slice(0, 8).map(activityRow).join('')}</div>` : emptyHTML({ icon: 'history', title: '暂无动态' })}
      </gh-box>
    </div>

    <div class="split-2-even">
      <gh-box title="成员负载">
        ${workloadTable(workload)}
      </gh-box>
      <gh-box title="近 14 天趋势">
        ${trendChart(trend)}
      </gh-box>
    </div>`;

  onView(view, 'click', (event) => {
    const act = event.target.closest('[data-act]');
    if (act) {
      const name = act.getAttribute('data-act');
      if (name === 'refresh') { invalidateTeam(slug); render(view, route); return; }
      if (name === 'new-issue') { navigate(`/${slug}/issues?new=1`); return; }
    }
    const ms = event.target.closest('[data-ms]');
    if (ms) navigate(`/${slug}/milestones`);
  });
}
