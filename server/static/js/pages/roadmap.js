/* TeamFlow · 路线图 */

import { api } from '../api.js';
import { S } from '../store.js';
import { navigate } from '../router.js';
import { esc, pageHead, emptyHTML, todayISO, daysBetween,
  onView,
} from '../ui.js';

function parseDay(value) {
  const text = String(value || '').slice(0, 10);
  const time = Date.parse(`${text}T00:00:00`);
  return Number.isFinite(time) ? time : null;
}

function monthTicks(startTime, endTime) {
  const ticks = [];
  const cursor = new Date(startTime);
  cursor.setDate(1);
  cursor.setMonth(cursor.getMonth() + 1);
  const total = endTime - startTime || 1;
  while (cursor.getTime() <= endTime) {
    const left = ((cursor.getTime() - startTime) / total) * 100;
    ticks.push({ left, label: `${cursor.getFullYear()}-${String(cursor.getMonth() + 1).padStart(2, '0')}` });
    cursor.setMonth(cursor.getMonth() + 1);
  }
  return ticks;
}

export async function render(view, route) {
  const slug = S.detail.slug;
  let data;
  try {
    data = await api.get(`/api/teams/${encodeURIComponent(slug)}/roadmap`);
  } catch (error) {
    view.innerHTML = `<div class="error-box"><gh-icon name="alert" size="24"></gh-icon>
      <div class="error-box__title">路线图加载失败</div>
      <div class="error-box__msg">${esc(error.message || '')}</div>
      <gh-btn size="sm" data-act="retry">重试</gh-btn></div>`;
    onView(view, 'click', (event) => {
      if (event.target.closest('[data-act="retry"]')) render(view, route);
    });
    return;
  }

  const milestones = Array.isArray(data.milestones) ? data.milestones : [];
  const scheduled = milestones.filter((milestone) => milestone.kind !== 'continuous' && (milestone.start_date || milestone.due_date));
  const continuous = milestones.filter((milestone) => milestone.kind === 'continuous');

  if (!milestones.length) {
    view.innerHTML = `${pageHead('路线图', '按时间轴查看里程碑安排', '')}
      ${emptyHTML({ icon: 'project-roadmap', title: '暂无路线图', desc: '创建带起止日期的里程碑后，这里会显示时间轴。' })}`;
    return;
  }

  const times = [];
  scheduled.forEach((milestone) => {
    const start = parseDay(milestone.start_date) ?? parseDay(milestone.due_date);
    const due = parseDay(milestone.due_date) ?? parseDay(milestone.start_date);
    if (start !== null) times.push(start);
    if (due !== null) times.push(due);
  });
  const today = parseDay(todayISO());
  times.push(today);
  const startTime = Math.min(...times);
  const endTime = Math.max(...times);
  const span = Math.max(endTime - startTime, 86400000 * 7);
  const offset = (time) => Math.max(0, Math.min(100, ((time - startTime) / span) * 100));
  const todayLeft = offset(today);
  const ticks = monthTicks(startTime, startTime + span);

  const rows = scheduled.map((milestone) => {
    const start = parseDay(milestone.start_date) ?? parseDay(milestone.due_date) ?? startTime;
    const due = parseDay(milestone.due_date) ?? parseDay(milestone.start_date) ?? startTime;
    const left = offset(Math.min(start, due));
    const right = offset(Math.max(start, due));
    const width = Math.max(right - left, 3);
    const closed = milestone.state === 'closed';
    const progress = Math.max(0, Math.min(100, Number(milestone.progress || 0)));
    const done = milestone.closed_count ?? milestone.closed ?? 0;
    const total = milestone.total ?? 0;
    const short = (value) => (value ? String(value).slice(5, 10) : '—');
    const range = `${short(milestone.start_date)} → ${short(milestone.due_date)}`;
    const tip = `${milestone.title}：${range} · ${done}/${total}`;
    return `<div class="timeline__row">
      <div class="timeline__label">
        <a href="/${esc(slug)}/issues?milestone=${esc(milestone.id)}">${esc(milestone.title)}</a>
        <div class="small muted">${done}/${total} · ${progress}% · ${esc(range)}</div>
      </div>
      <div class="timeline__track">
        <div class="timeline__bar${closed ? ' is-closed' : ''}" style="left:${left}%;width:${width}%" title="${esc(tip)}" aria-label="${esc(tip)}">
          <span class="timeline__bar-fill" style="width:${closed ? 100 : progress}%"></span>
        </div>
      </div>
    </div>`;
  }).join('');

  const continuousRows = continuous.map((milestone) => `<div class="row" style="padding:6px 0">
      <gh-icon name="sync" size="14"></gh-icon>
      <a class="grow" href="/${esc(slug)}/issues?milestone=${esc(milestone.id)}">${esc(milestone.title)}</a>
      <span class="small muted">${milestone.closed_count ?? milestone.closed ?? 0}/${milestone.total ?? 0} 已完成</span>
      <gh-progress value="${Math.round(Number(milestone.progress || 0))}" thin tone="accent" style="max-width:160px"></gh-progress>
    </div>`).join('');

  view.innerHTML = `${pageHead('路线图', '里程碑在时间轴上的分布，竖线为今天', `
      <gh-btn size="sm" data-act="refresh"><gh-icon name="sync" size="14"></gh-icon> 刷新</gh-btn>
    `)}
    <gh-box title="时间轴">
      <div class="timeline">
        <div class="timeline__row timeline__row--axis">
          <div class="timeline__label"></div>
          <div class="timeline__track">
            ${ticks.map((tick) => `<span class="timeline__tick" style="left:${tick.left}%">${esc(tick.label)}</span>`).join('')}
          </div>
        </div>
        <div class="timeline__row timeline__row--today">
          <div class="timeline__label small muted">今天 ${esc(todayISO())}</div>
          <div class="timeline__track"><span class="timeline__today" style="left:${todayLeft}%"></span></div>
        </div>
        ${rows || '<div class="small muted" style="padding:12px 0">还没有带日期的里程碑。</div>'}
      </div>
    </gh-box>
    ${continuous.length ? `<gh-box title="持续型里程碑">${continuousRows}</gh-box>` : ''}`;

  onView(view, 'click', (event) => {
    if (event.target.closest('[data-act="refresh"]')) render(view, route);
  });
}

export { navigate, daysBetween };
