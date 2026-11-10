/* TeamFlow · 议题表单（新建 / 编辑 / 删除） */

import { api } from './api.js';
import { S } from './store.js';
import {
  openDialog, fieldHTML, optionsHTML, checkboxGroupHTML, confirmAction, toast, esc,
} from './ui.js';

const metaCache = new Map();

export async function loadMeta(slug, options = {}) {
  const { force = false } = options;
  if (!force && metaCache.has(slug)) return metaCache.get(slug);
  const [milestoneData, labelData] = await Promise.all([
    api.get(`/api/teams/${encodeURIComponent(slug)}/milestones`).catch(() => ({ milestones: [] })),
    api.get(`/api/teams/${encodeURIComponent(slug)}/labels`).catch(() => ({ labels: [] })),
  ]);
  const meta = {
    milestones: Array.isArray(milestoneData.milestones) ? milestoneData.milestones : [],
    labels: Array.isArray(labelData.labels) ? labelData.labels : [],
  };
  metaCache.set(slug, meta);
  return meta;
}

export function invalidateMeta(slug) {
  if (slug) metaCache.delete(slug);
  else metaCache.clear();
}

function toId(value) {
  const text = String(value === null || value === undefined ? '' : value).trim();
  if (!text) return null;
  const num = Number(text);
  return Number.isFinite(num) && num > 0 ? num : null;
}

function toIdList(value) {
  if (Array.isArray(value)) return value.map(toId).filter((id) => id !== null);
  const one = toId(value);
  return one === null ? [] : [one];
}

export const STATUS_OPTIONS = [
  { value: 'todo', label: '待办' },
  { value: 'in_progress', label: '进行中' },
  { value: 'done', label: '已完成' },
];

export const PRIORITY_OPTIONS = [
  { value: 'high', label: '高优先级' },
  { value: 'medium', label: '中优先级' },
  { value: 'low', label: '低优先级' },
];

export async function openIssueDialog(options = {}) {
  const { slug = S.detail.slug, issue = null, defaults = {}, onSaved = null } = options;
  const [meta, issueData] = await Promise.all([
    loadMeta(slug),
    api.get(`/api/teams/${encodeURIComponent(slug)}/issues`, { state: 'all', limit: 300 })
      .catch(() => ({ issues: [] })),
  ]);
  const allIssues = Array.isArray(issueData.issues) ? issueData.issues : [];
  const members = Array.isArray(S.detail && S.detail.members) ? S.detail.members : [];
  const current = issue || {};
  const value = (key, fallback) => (current[key] !== undefined && current[key] !== null ? current[key] : (fallback !== undefined ? fallback : defaults[key]));

  const milestoneOptions = [{ value: '', label: '不关联里程碑' }]
    .concat(meta.milestones.map((ms) => ({ value: ms.id, label: `#${ms.number} ${ms.title}` })));
  const assigneeOptions = [{ value: '', label: '未指派' }]
    .concat(members.map((member) => ({ value: member.id, label: member.display_name || member.username })));
  const selectedLabels = Array.isArray(current.labels) ? current.labels.map((label) => label.id) : [];

  /* 可选父议题：排除自己与自己的所有后代，避免前端就能造出环 */
  const blocked = new Set();
  if (issue && issue.id) {
    blocked.add(issue.id);
    let added = true;
    while (added) {
      added = false;
      for (const item of allIssues) {
        if (item.parent_id && blocked.has(item.parent_id) && !blocked.has(item.id)) {
          blocked.add(item.id);
          added = true;
        }
      }
    }
  }
  const parentOptions = [{ value: '', label: '无（顶层议题）' }]
    .concat(allIssues
      .filter((item) => !blocked.has(item.id))
      .map((item) => ({ value: item.id, label: `#${item.number} ${item.title}` })));
  const parentDefault = current.parent_id || defaults.parent_id || '';

  const body = `
    <gh-field label="标题" hint="一句话说明要解决的问题">
      <gh-input name="title" icon="issue-opened" maxlength="200" placeholder="例如：周计划页面支持拖拽排序"></gh-input>
    </gh-field>
    <gh-field label="描述" hint="支持 Markdown">
      <gh-textarea name="body" rows="7" placeholder="补充背景、验收标准或复现步骤…"></gh-textarea>
    </gh-field>
    <div class="form-grid">
      ${fieldHTML({ label: '状态', name: 'status', type: 'select', options: STATUS_OPTIONS, value: value('status', 'todo') })}
      ${fieldHTML({ label: '优先级', name: 'priority', type: 'select', options: PRIORITY_OPTIONS, value: value('priority', 'medium') })}
      ${fieldHTML({ label: '里程碑', name: 'milestone_id', type: 'select', options: milestoneOptions, value: current.milestone_id || '' })}
      ${fieldHTML({ label: '指派给', name: 'assignee_id', type: 'select', options: assigneeOptions, value: current.assignee_id || '' })}
      ${fieldHTML({ label: '父议题', name: 'parent', type: 'select', options: parentOptions, value: parentDefault, hint: '最多 3 层；父议题的进度只统计子议题' })}
      ${fieldHTML({ label: '预估工时', name: 'estimate_hours', type: 'number', value: current.estimate_hours ?? '', placeholder: '4' })}
      ${fieldHTML({ label: '截止日期', name: 'due_date', type: 'date' })}
      ${fieldHTML({ label: '计划周', name: 'plan_week', placeholder: '2026-W45' })}
    </div>
    <gh-field label="标签">${meta.labels.length ? checkboxGroupHTML('label_ids', meta.labels.map((label) => ({ value: label.id, label: label.name })), selectedLabels) : '<div class="small muted">该团队还没有标签，可以在设置页创建。</div>'}</gh-field>
  `;

  const dialog = openDialog({
    title: issue ? `编辑议题 #${issue.number}` : '新建议题',
    okText: issue ? '保存' : '创建',
    width: 'wide',
    body,
    onMount: (node) => {
      const title = node.querySelector('[name="title"]');
      if (title) title.value = current.title || defaults.title || '';
      const text = node.querySelector('[name="body"]');
      if (text) text.value = current.body || defaults.body || '';
      const due = node.querySelector('[name="due_date"]');
      if (due) {
        const date = current.due_date ? String(current.due_date).slice(0, 10) : (defaults.due_date || '');
        due.value = /^\d{4}-\d{2}-\d{2}$/.test(date) ? date : '';
      }
      const week = node.querySelector('[name="plan_week"]');
      if (week) week.value = current.plan_week || defaults.plan_week || '';
    },
    onSubmit: async (values) => {
      const title = String(values.title || '').trim();
      if (!title) { toast('请填写议题标题', 'err'); return false; }
      const parentValue = String(values.parent ?? '').trim();
      const payload = {
        title,
        body: String(values.body || ''),
        status: values.status || 'todo',
        priority: values.priority || 'medium',
        milestone_id: toId(values.milestone_id),
        assignee_id: toId(values.assignee_id),
        estimate_hours: values.estimate_hours === '' || values.estimate_hours === undefined
          ? null : Number(values.estimate_hours),
        due_date: values.due_date ? String(values.due_date).slice(0, 10) : null,
        plan_week: values.plan_week ? String(values.plan_week).trim() : null,
        label_ids: toIdList(values.label_ids),
      };
      if (parentValue) payload.parent = toId(parentValue);
      else if (current.parent_id) payload.parent = '';
      const data = issue
        ? await api.patch(`/api/teams/${encodeURIComponent(slug)}/issues/${issue.number}`, payload)
        : await api.post(`/api/teams/${encodeURIComponent(slug)}/issues`, payload);
      toast(issue ? '议题已保存' : '议题已创建');
      if (onSaved) await onSaved(data.issue, dialog);
      return true;
    },
  });
  return dialog;
}

export async function deleteIssue(slug, issue, onDone) {
  const ok = await confirmAction({
    title: `删除议题 #${issue.number}`,
    message: `确定删除「${issue.title}」吗？该操作不可撤销。`,
    okText: '删除',
    danger: true,
  });
  if (!ok) return false;
  await api.del(`/api/teams/${encodeURIComponent(slug)}/issues/${issue.number}`);
  toast('议题已删除');
  if (onDone) onDone();
  return true;
}

export { esc };
