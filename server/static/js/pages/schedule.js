/* TeamFlow · 日程 */

import { api } from '../api.js';
import { S, actorMember } from '../store.js';
import {
  esc, pageHead, avatarHTML, emptyHTML, toast, confirmAction, openDialog, fieldHTML,
  checkboxGroupHTML, WEEKDAYS, weekdayName, fmtDate, todayISO,
  onView,
} from '../ui.js';

const KIND_OPTIONS = [
  { value: 'recurring', label: '每周重复' },
  { value: 'once', label: '单次' },
];

function mondayOf(date) {
  const base = new Date(date.getTime());
  const offset = (base.getDay() + 6) % 7;
  base.setDate(base.getDate() - offset);
  base.setHours(0, 0, 0, 0);
  return base;
}

function isoOf(date) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function entryChips(entry) {
  return `${String(entry.start_time || '').slice(0, 5)}–${String(entry.end_time || '').slice(0, 5)}`;
}

function entryHTML(entry, options = {}) {
  const color = entry.color || '#0969da';
  const ownerName = entry.user_name || '成员';
  const names = (entry.participants || [])
    .map((person) => person.name || person.display_name || person.username || '')
    .filter(Boolean);
  const owner = { display_name: ownerName };
  return `<div class="cal-item" data-id="${esc(entry.id)}" style="--tf-color:${esc(color)}">
    <div class="cal-item__head">
      <span class="cal-item__time">${esc(entryChips(entry))}</span>
      <span class="grow"></span>
      ${options.showOwner ? avatarHTML(owner, 14) : ''}
    </div>
    <div class="cal-item__title">${esc(entry.title)}</div>
    <div class="cal-item__people">${esc(names.length ? names.join('、') : ownerName)}</div>
  </div>`;
}

function entryPlaced(entry, weekDates) {
  if (entry.kind === 'recurring') {
    if (entry.start_date && String(entry.start_date).slice(0, 10) > weekDates[6]) return false;
    if (entry.end_date && String(entry.end_date).slice(0, 10) < weekDates[0]) return false;
    return true;
  }
  return weekDates.includes(String(entry.date || '').slice(0, 10));
}

export function openScheduleDialog(entry = null, onSaved = null) {
  const slug = S.detail.slug;
  const members = Array.isArray(S.detail.members) ? S.detail.members : [];
  const current = entry || {};
  const weekdayOptions = WEEKDAYS.map((label, index) => ({ value: index, label }));
  const participantItems = members.map((member) => ({ value: member.id, label: member.display_name || member.username }));
  const ownerOptions = members.map((member) => ({
    value: member.id,
    label: `${member.display_name || member.username}${member.title ? ` · ${member.title}` : ''}`,
  }));
  const actor = actorMember();
  const ownerDefault = current.user_id || (actor ? actor.id : '') || (S.user ? S.user.id : '') || (members[0] ? members[0].id : '');

  const body = `
    ${fieldHTML({ label: '标题', name: 'title', placeholder: '例如：周会 / 客户演示', maxlength: 200 })}
    <div class="form-grid">
      ${fieldHTML({ label: '类型', name: 'kind', type: 'select', options: KIND_OPTIONS, value: current.kind || 'recurring' })}
      ${ownerOptions.length ? fieldHTML({ label: '负责人', name: 'user_id', type: 'select', options: ownerOptions, value: ownerDefault }) : ''}
      ${fieldHTML({ label: '星期', name: 'weekday', type: 'select', options: weekdayOptions, value: current.weekday ?? 0 })}
      ${fieldHTML({ label: '日期（单次）', name: 'date', type: 'date' })}
      ${fieldHTML({ label: '开始时间', name: 'start_time', type: 'time' })}
      ${fieldHTML({ label: '结束时间', name: 'end_time', type: 'time' })}
      ${fieldHTML({ label: '颜色', name: 'color', type: 'color' })}
      ${fieldHTML({ label: '开始日期（重复范围）', name: 'start_date', type: 'date' })}
      ${fieldHTML({ label: '结束日期（重复范围）', name: 'end_date', type: 'date' })}
    </div>
    ${fieldHTML({ label: '备注', name: 'note', type: 'textarea', rows: 3, placeholder: '会议链接、准备事项…' })}
    <gh-field label="参与人">${participantItems.length ? checkboxGroupHTML('participant_ids', participantItems, (current.participants || []).map((p) => p.id)) : '<div class="small muted">还没有其他成员。</div>'}</gh-field>`;

  return openDialog({
    title: entry ? '编辑日程' : '新建日程',
    okText: entry ? '保存' : '创建',
    width: 'wide',
    body,
    onMount: (dialog) => {
      const set = (name, value) => {
        const node = dialog.querySelector(`[name="${name}"]`);
        if (node) node.value = value === null || value === undefined ? '' : String(value);
      };
      set('title', current.title || '');
      set('note', current.note || '');
      set('date', current.date ? String(current.date).slice(0, 10) : todayISO());
      set('start_time', current.start_time || '09:30');
      set('end_time', current.end_time || '10:00');
      set('color', current.color || '#0969da');
      set('start_date', current.start_date ? String(current.start_date).slice(0, 10) : '');
      set('end_date', current.end_date ? String(current.end_date).slice(0, 10) : '');
    },
    onSubmit: async (values) => {
      const title = String(values.title || '').trim();
      if (!title) { toast('请填写日程标题', 'err'); return false; }
      const kind = values.kind || 'recurring';
      const payload = {
        title,
        kind,
        note: String(values.note || ''),
        start_time: values.start_time ? String(values.start_time).slice(0, 5) : '',
        end_time: values.end_time ? String(values.end_time).slice(0, 5) : '',
        color: values.color || '#0969da',
        start_date: values.start_date ? String(values.start_date).slice(0, 10) : null,
        end_date: values.end_date ? String(values.end_date).slice(0, 10) : null,
        participant_ids: Array.isArray(values.participant_ids)
          ? values.participant_ids.map(Number).filter(Boolean)
          : (values.participant_ids ? [Number(values.participant_ids)].filter(Boolean) : []),
      };
      if (kind === 'recurring') {
        payload.weekday = Number(values.weekday || 0);
        payload.date = null;
      } else {
        payload.date = values.date ? String(values.date).slice(0, 10) : null;
      }
      if (!payload.start_time || !payload.end_time) { toast('请填写开始与结束时间', 'err'); return false; }
      if (payload.end_time <= payload.start_time) { toast('结束时间需要晚于开始时间', 'err'); return false; }
      if (kind === 'once' && !payload.date) { toast('单次日程需要选择日期', 'err'); return false; }
      const owner = Number(values.user_id);
      if (owner) payload.user_id = owner;
      else if (!entry) {
        toast('请先在侧栏选择操作身份，或指定负责人', 'err');
        return false;
      }
      if (entry) {
        await api.patch(`/api/teams/${encodeURIComponent(slug)}/schedules/${entry.id}`, payload);
        toast('日程已保存');
      } else {
        await api.post(`/api/teams/${encodeURIComponent(slug)}/schedules`, payload);
        toast('日程已创建');
      }
      if (onSaved) await onSaved();
      return true;
    },
  });
}

export async function render(view, route) {
  const slug = S.detail.slug;
  let data;
  try {
    data = await api.get(`/api/teams/${encodeURIComponent(slug)}/schedules`);
  } catch (error) {
    view.innerHTML = `<div class="error-box"><gh-icon name="alert" size="24"></gh-icon>
      <div class="error-box__title">日程加载失败</div>
      <div class="error-box__msg">${esc(error.message || '')}</div>
      <gh-btn size="sm" data-act="retry">重试</gh-btn></div>`;
    onView(view, 'click', (event) => {
      if (event.target.closest('[data-act="retry"]')) render(view, route);
    });
    return;
  }

  const entries = Array.isArray(data.schedules) ? data.schedules : [];
  const canEdit = !!data.can_edit || S.canEdit;
  const monday = mondayOf(new Date());
  const weekDates = [];
  for (let i = 0; i < 7; i += 1) {
    const day = new Date(monday.getTime());
    day.setDate(monday.getDate() + i);
    weekDates.push(isoOf(day));
  }

  const columns = WEEKDAYS.map((label, index) => {
    const dayEntries = entries
      .filter((entry) => entryPlaced(entry, weekDates))
      .filter((entry) => (entry.kind === 'recurring' ? Number(entry.weekday) === index : String(entry.date || '').slice(0, 10) === weekDates[index]))
      .sort((a, b) => String(a.start_time || '').localeCompare(String(b.start_time || '')));
    const isToday = weekDates[index] === todayISO();
    return `<div class="cal-col${isToday ? ' is-today' : ''}">
      <div class="cal-col__head">
        <span>${esc(label)}</span>
        <span class="small muted">${esc(weekDates[index].slice(5))}</span>
      </div>
      <div class="cal-col__body">
        ${dayEntries.length ? dayEntries.map((entry) => entryHTML(entry, { showOwner: true })).join('') : '<div class="cal-empty small muted">—</div>'}
      </div>
    </div>`;
  }).join('');

  const listRows = entries.slice().sort((a, b) => {
    const keyA = a.kind === 'recurring' ? `1-${a.weekday}-${a.start_time}` : `0-${a.date}-${a.start_time}`;
    const keyB = b.kind === 'recurring' ? `1-${b.weekday}-${b.start_time}` : `0-${b.date}-${b.start_time}`;
    return String(keyA).localeCompare(String(keyB));
  }).map((entry) => `<div class="row" style="padding:8px 0;border-bottom:1px solid var(--gh-border-muted)">
      <span class="cal-dot" style="background:${esc(entry.color || '#0969da')}"></span>
      <div class="grow">
        <div class="row" style="gap:8px;align-items:baseline">
          <b>${esc(entry.title)}</b>
          <span class="small muted">${entry.kind === 'recurring' ? `每${esc(weekdayName(entry.weekday))}` : esc(fmtDate(entry.date))} · ${esc(entryChips(entry))}</span>
        </div>
        <div class="small muted">${entry.note ? esc(entry.note) : ''}${entry.participants && entry.participants.length ? ` · ${entry.participants.map((p) => esc(p.name)).join('、')}` : ''}</div>
      </div>
      ${canEdit ? `<gh-btn size="sm" variant="invisible" data-act="edit" data-id="${esc(entry.id)}"><gh-icon name="pencil" size="12"></gh-icon></gh-btn>
        <gh-btn size="sm" variant="invisible" data-act="delete" data-id="${esc(entry.id)}"><gh-icon name="trash" size="12"></gh-icon></gh-btn>` : ''}
    </div>`).join('');

  const icsUrl = data.ics_token ? `${location.origin}/api/teams/${encodeURIComponent(slug)}/schedules.ics?token=${encodeURIComponent(data.ics_token)}` : '';

  view.innerHTML = `${pageHead('日程', '每周固定安排与单次日程，支持订阅到本地日历', `
      <gh-btn size="sm" data-act="refresh"><gh-icon name="sync" size="14"></gh-icon> 刷新</gh-btn>
      ${canEdit ? '<gh-btn size="sm" variant="primary" data-act="new"><gh-icon name="plus" size="14"></gh-icon> 新建日程</gh-btn>' : ''}
    `)}
    ${icsUrl ? `<gh-alert type="note" dismissible>订阅地址（可添加到本地日历）：<span class="mono small">${esc(icsUrl)}</span> <gh-btn size="sm" variant="invisible" data-act="copy-ics">复制</gh-btn></gh-alert>` : ''}
    <gh-box title="${esc(`${fmtDate(weekDates[0])} — ${fmtDate(weekDates[6])} 这一周`)}">
      <div class="cal">${columns}</div>
    </gh-box>
    <gh-box title="全部日程 (${entries.length})">
      ${entries.length ? listRows : emptyHTML({ icon: 'calendar', title: '还没有日程', desc: canEdit ? '创建每周站会或单次会议安排。' : '该团队还没有安排日程。' })}
    </gh-box>`;

  onView(view, 'click', async (event) => {
    const act = event.target.closest('[data-act]');
    if (!act) return;
    const name = act.getAttribute('data-act');
    try {
      if (name === 'new') { openScheduleDialog(null, () => render(view, route)); return; }
      if (name === 'refresh' || name === 'retry') { render(view, route); return; }
      if (name === 'edit') {
        const entry = entries.find((item) => String(item.id) === String(act.getAttribute('data-id')));
        if (entry) openScheduleDialog(entry, () => render(view, route));
        return;
      }
      if (name === 'delete') {
        const entry = entries.find((item) => String(item.id) === String(act.getAttribute('data-id')));
        if (!entry) return;
        const ok = await confirmAction({ title: '删除日程', message: `确定删除「${entry.title}」吗？`, okText: '删除', danger: true });
        if (!ok) return;
        await api.del(`/api/teams/${encodeURIComponent(slug)}/schedules/${entry.id}`);
        toast('日程已删除');
        render(view, route);
        return;
      }
      if (name === 'copy-ics' && icsUrl) {
        try { await navigator.clipboard.writeText(icsUrl); toast('订阅地址已复制'); }
        catch { toast('复制失败，请手动选择地址', 'err'); }
        return;
      }
    } catch (error) {
      toast(error.message || '操作失败', 'err');
    }
  });
}
