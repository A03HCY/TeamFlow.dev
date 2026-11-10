/* TeamFlow · 里程碑 */

import { api } from '../api.js';
import { S } from '../store.js';
import { navigate } from '../router.js';
import {
  esc, pageHead, fmtDate, emptyHTML, toast, confirmAction, openDialog,
  fieldHTML, optionsHTML,
  onView,
} from '../ui.js';

const KIND_OPTIONS = [
  { value: 'scheduled', label: '计划型（有起止日期）' },
  { value: 'continuous', label: '持续型（长期目标）' },
];

const STATE_OPTIONS = [
  { value: 'open', label: '进行中' },
  { value: 'closed', label: '已完成' },
];

function milestoneCard(milestone, slug) {
  const progress = Math.max(0, Math.min(100, Math.round(Number(milestone.progress || 0))));
  const closed = milestone.state === 'closed';
  const dates = milestone.kind === 'continuous'
    ? '持续进行'
    : [milestone.start_date ? fmtDate(milestone.start_date) : null, milestone.due_date ? fmtDate(milestone.due_date) : null].filter(Boolean).join(' → ') || '未设置日期';
  return `<div class="ms-card${closed ? ' is-closed' : ''}">
    <div class="row">
      <gh-icon name="milestone" size="16"></gh-icon>
      <a class="ms-card__title grow" href="/${esc(slug)}/issues?milestone=${esc(milestone.id)}">${esc(milestone.title)}</a>
      <span class="mono small muted">#${esc(milestone.number)}</span>
    </div>
    ${milestone.description ? `<div class="ms-card__desc small muted">${esc(milestone.description)}</div>` : ''}
    ${milestone.acceptance ? `<div class="ms-card__acceptance">
      <div class="small muted row" style="gap:4px"><gh-icon name="tasklist" size="12"></gh-icon> 验收标准</div>
      <div class="ms-card__acceptance-body" data-acceptance="${esc(milestone.number)}"></div>
    </div>` : ''}
    <div class="row small muted">
      <span>${esc(dates)}</span>
      <span class="grow"></span>
      <span>${milestone.closed_count ?? milestone.closed ?? 0}/${milestone.total ?? 0} 已完成</span>
    </div>
    <gh-progress value="${progress}" tone="${closed || progress >= 100 ? 'ok' : 'accent'}" show-value></gh-progress>
    <div class="row">
      <gh-badge state="${closed ? 'closed' : 'open'}" dot>${closed ? '已完成' : '进行中'}</gh-badge>
      <gh-badge state="${milestone.kind === 'continuous' ? 'accent' : 'open'}">${milestone.kind === 'continuous' ? '持续型' : '计划型'}</gh-badge>
      <span class="grow"></span>
      ${S.canEdit ? `<gh-btn size="sm" variant="invisible" data-act="edit" data-number="${esc(milestone.number)}"><gh-icon name="pencil" size="12"></gh-icon> 编辑</gh-btn>
        <gh-btn size="sm" variant="invisible" data-act="delete" data-number="${esc(milestone.number)}"><gh-icon name="trash" size="12"></gh-icon> 删除</gh-btn>` : ''}
    </div>
  </div>`;
}

export function openMilestoneDialog(milestone = null, onSaved = null) {
  const slug = S.detail.slug;
  const current = milestone || {};
  const body = `
    ${fieldHTML({ label: '标题', name: 'title', placeholder: '例如：v2.0 发布', maxlength: 200 })}
    ${fieldHTML({ label: '描述', name: 'description', type: 'textarea', rows: 3, placeholder: '这个里程碑要达成什么？' })}
    ${fieldHTML({ label: '验收标准', name: 'acceptance', type: 'textarea', rows: 4, placeholder: '- [ ] 支持邮箱登录\n- [ ] 补齐集成测试' , hint: '一行一条，验收标准会成为任务树提案的依据' })}
    <div class="form-grid">
      ${fieldHTML({ label: '类型', name: 'kind', type: 'select', options: KIND_OPTIONS, value: current.kind || 'scheduled' })}
      ${fieldHTML({ label: '状态', name: 'state', type: 'select', options: STATE_OPTIONS, value: current.state || 'open' })}
      ${fieldHTML({ label: '开始日期', name: 'start_date', type: 'date' })}
      ${fieldHTML({ label: '截止日期', name: 'due_date', type: 'date' })}
    </div>
    <div class="small muted">持续型里程碑不需要截止日期，适合长期方向。</div>`;

  return openDialog({
    title: milestone ? `编辑里程碑 #${milestone.number}` : '新建里程碑',
    okText: milestone ? '保存' : '创建',
    body,
    onMount: (dialog) => {
      const title = dialog.querySelector('[name="title"]');
      if (title) title.value = current.title || '';
      const desc = dialog.querySelector('[name="description"]');
      if (desc) desc.value = current.description || '';
      const acceptance = dialog.querySelector('[name="acceptance"]');
      if (acceptance) acceptance.value = current.acceptance || '';
      const start = dialog.querySelector('[name="start_date"]');
      if (start) start.value = current.start_date ? String(current.start_date).slice(0, 10) : '';
      const due = dialog.querySelector('[name="due_date"]');
      if (due) due.value = current.due_date ? String(current.due_date).slice(0, 10) : '';
    },
    onSubmit: async (values) => {
      const title = String(values.title || '').trim();
      if (!title) { toast('请填写里程碑标题', 'err'); return false; }
      const payload = {
        title,
        description: String(values.description || ''),
        acceptance: String(values.acceptance || ''),
        kind: values.kind || 'scheduled',
        start_date: values.start_date ? String(values.start_date).slice(0, 10) : null,
        due_date: values.due_date ? String(values.due_date).slice(0, 10) : null,
      };
      if (milestone) {
        payload.state = values.state || current.state || 'open';
        await api.patch(`/api/teams/${encodeURIComponent(slug)}/milestones/${milestone.number}`, payload);
        toast('里程碑已保存');
      } else {
        await api.post(`/api/teams/${encodeURIComponent(slug)}/milestones`, payload);
        toast('里程碑已创建');
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
    data = await api.get(`/api/teams/${encodeURIComponent(slug)}/milestones`);
  } catch (error) {
    view.innerHTML = `<div class="error-box"><gh-icon name="alert" size="24"></gh-icon>
      <div class="error-box__title">里程碑加载失败</div>
      <div class="error-box__msg">${esc(error.message || '')}</div>
      <gh-btn size="sm" data-act="retry">重试</gh-btn></div>`;
    onView(view, 'click', (event) => {
      if (event.target.closest('[data-act="retry"]')) render(view, route);
    });
    return;
  }

  const milestones = Array.isArray(data.milestones) ? data.milestones : [];
  const canEdit = !!data.can_edit || S.canEdit;
  const open = milestones.filter((milestone) => milestone.state !== 'closed');
  const closed = milestones.filter((milestone) => milestone.state === 'closed');

  view.innerHTML = `${pageHead('里程碑', '按里程碑聚合议题进度，跟踪阶段性目标', `
      <gh-btn size="sm" data-act="refresh"><gh-icon name="sync" size="14"></gh-icon> 刷新</gh-btn>
      ${canEdit ? '<gh-btn size="sm" variant="primary" data-act="new"><gh-icon name="plus" size="14"></gh-icon> 新建里程碑</gh-btn>' : ''}
    `)}
    ${milestones.length ? `
      <div class="ms-grid">${open.map((milestone) => milestoneCard(milestone, slug)).join('')}</div>
      ${closed.length ? `<gh-box title="已完成 (${closed.length})">
        <div class="ms-grid">${closed.map((milestone) => milestoneCard(milestone, slug)).join('')}</div>
      </gh-box>` : ''}
    ` : emptyHTML({ icon: 'milestone', title: '还没有里程碑', desc: canEdit ? '创建第一个里程碑来组织议题。' : '该团队还没有创建里程碑。' })}`;

  view.querySelectorAll('[data-acceptance]').forEach((host) => {
    const number = host.getAttribute('data-acceptance');
    const milestone = milestones.find((item) => String(item.number) === String(number));
    if (!milestone || !milestone.acceptance) return;
    const md = document.createElement('gh-md');
    md.textContent = milestone.acceptance;
    host.append(md);
  });

  onView(view, 'click', async (event) => {
    const act = event.target.closest('[data-act]');
    if (!act) return;
    const name = act.getAttribute('data-act');
    try {
      if (name === 'new') {
        openMilestoneDialog(null, () => render(view, route));
        return;
      }
      if (name === 'refresh' || name === 'retry') { render(view, route); return; }
      if (name === 'edit') {
        const number = act.getAttribute('data-number');
        const milestone = milestones.find((item) => String(item.number) === String(number));
        if (milestone) openMilestoneDialog(milestone, () => render(view, route));
        return;
      }
      if (name === 'delete') {
        const number = act.getAttribute('data-number');
        const milestone = milestones.find((item) => String(item.number) === String(number));
        if (!milestone) return;
        const ok = await confirmAction({
          title: `删除里程碑 #${milestone.number}`,
          message: `确定删除「${milestone.title}」吗？关联议题不会被删除。`,
          okText: '删除',
          danger: true,
        });
        if (!ok) return;
        await api.del(`/api/teams/${encodeURIComponent(slug)}/milestones/${milestone.number}`);
        toast('里程碑已删除');
        render(view, route);
        return;
      }
    } catch (error) {
      toast(error.message || '操作失败', 'err');
    }
  });
}

export { optionsHTML, navigate };
