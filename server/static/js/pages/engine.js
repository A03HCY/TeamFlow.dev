/* TeamFlow · 推理引擎（P1：端侧接入 + 进度洞察收件箱） */

import { api } from '../api.js';
import { S } from '../store.js';
import {
  esc, pageHead, emptyHTML, toast, confirmAction, openDialog, fieldHTML, fmtDateTime, timeAgo,
  onView,
} from '../ui.js';

const STATE_META = {
  online: { label: '在线', badge: 'open' },
  offline: { label: '离线', badge: 'closed' },
  pending: { label: '待接入', badge: 'warn' },
  revoked: { label: '已吊销', badge: 'danger' },
};

const STATUS_TEXT = {
  todo: '待办',
  in_progress: '进行中',
  done: '已完成',
};

/* 提案类型（P2.5）：结构类动作永远要人确认后才落库 */
const KIND_META = {
  split: { label: '拆分建议', icon: 'git-branch', hint: '把一个议题拆成子议题' },
  plan: { label: '任务树', icon: 'tasklist', hint: '按验收标准生成任务' },
  criteria: { label: '验收标准', icon: 'shield-check', hint: '为里程碑补验收标准' },
  assignment: { label: '负责人建议', icon: 'person', hint: '按技能与负载推荐' },
  progress: { label: '进度洞察', icon: 'sparkle', hint: '' },
};

const OP_TEXT = {
  create_issue: '新建议题',
  create_milestone: '新建里程碑',
  update_issue: '更新议题',
  update_milestone: '更新里程碑',
  close_issue: '关闭议题',
  assign: '指派负责人',
};

const PROPOSAL_KINDS = ['split', 'plan', 'criteria', 'assignment'];

function isProposal(insight) {
  return !!(insight && insight.kind && insight.kind !== 'progress');
}

function kindBadge(kind) {
  const meta = KIND_META[kind] || { label: kind, icon: 'sparkle' };
  const state = kind === 'split' ? 'accent' : kind === 'assignment' ? 'purple' : kind === 'criteria' ? 'warn' : 'open';
  return `<gh-badge state="${state}">${esc(meta.label)}</gh-badge>`;
}

function actionList(insight) {
  const suggestion = insight.suggestion || {};
  const actions = Array.isArray(suggestion.actions) ? suggestion.actions : [];
  if (!actions.length) return '';
  return `<div class="insight-card__actions">${actions.slice(0, 8).map((action) => `
    <div class="row" style="gap:6px;align-items:baseline">
      <gh-icon name="chevron-right" size="12"></gh-icon>
      <span class="small">${esc(OP_TEXT[action.op] || action.op)}</span>
      <span class="grow small">${esc(actionText(action))}</span>
    </div>`).join('')}</div>`;
}

function actionText(action) {
  if (!action) return '';
  if (action.op === 'create_issue') return action.title || '';
  if (action.op === 'update_issue' || action.op === 'close_issue') {
    return action.issue ? `#${action.issue}${action.status ? ` → ${STATUS_TEXT[action.status] || action.status}` : ''}` : '';
  }
  if (action.op === 'assign') return `${action.issue ? `#${action.issue} ` : ''}${action.assignee_name || action.assignee || ''}`;
  if (action.op === 'create_milestone') return action.title || '';
  if (action.op === 'update_milestone') return action.milestone ? `#${action.milestone}` : '';
  return '';
}

function risksBlock(insight) {
  const suggestion = insight.suggestion || {};
  const parts = [];
  if (Array.isArray(suggestion.warnings) && suggestion.warnings.length) {
    parts.push(`<gh-alert type="warning">${suggestion.warnings.map((item) => esc(item)).join('；')}</gh-alert>`);
  }
  if (Array.isArray(suggestion.assumptions) && suggestion.assumptions.length) {
    parts.push(`<div class="small muted">假设：${suggestion.assumptions.map((item) => esc(item)).join('；')}</div>`);
  }
  if (insight.applied && insight.applied.dry_run) {
    parts.push('<div class="small muted"><gh-icon name="eye" size="11"></gh-icon> 已做过一次预演（dry-run），尚未落库。</div>');
  }
  return parts.join('');
}

function deviceRow(device, canManage, paused = false) {
  const meta = paused ? { label: '已暂停', badge: 'warn' } : (STATE_META[device.state] || STATE_META.offline);
  const pauseBtn = device.online
    ? paused
      ? `<gh-btn size="sm" variant="invisible" data-act="resume" data-id="${esc(device.id)}">
           <gh-icon name="play" size="12"></gh-icon> 恢复
         </gh-btn>`
      : `<gh-btn size="sm" variant="invisible" data-act="pause" data-id="${esc(device.id)}">
           <gh-icon name="stop" size="12"></gh-icon> 暂停
         </gh-btn>`
    : '';
  const queryBtn = canManage && device.online
    ? `<gh-btn size="sm" variant="invisible" data-act="query" data-id="${esc(device.id)}"
         title="点名让端侧回报指定文件">
         <gh-icon name="search" size="12"></gh-icon> 查文件
       </gh-btn>`
    : '';
  const actions = canManage && device.state !== 'revoked'
    ? `${queryBtn}${pauseBtn}
       <gh-btn size="sm" variant="invisible" data-act="revoke" data-id="${esc(device.id)}">
         <gh-icon name="x" size="12"></gh-icon> 吊销
       </gh-btn>`
    : queryBtn;
  return `<tr>
    <td>
      <div class="row" style="gap:8px">
        <gh-icon name="desktop-download" size="16"></gh-icon>
        <div>
          <div><b>${esc(device.name || '未命名设备')}</b></div>
          <div class="small muted">${esc(device.platform || '未知平台')}${device.agent_version ? ` · v${esc(device.agent_version)}` : ''}</div>
        </div>
      </div>
    </td>
    <td><gh-badge state="${meta.badge}" dot>${meta.label}</gh-badge></td>
    <td class="mono small">${esc(device.event_count || 0)}</td>
    <td class="small muted">${device.last_seen_at ? esc(timeAgo(device.last_seen_at)) : '—'}</td>
    <td style="text-align:right">${actions}</td>
  </tr>`;
}

function evidenceChips(evidence) {
  const items = Array.isArray(evidence) ? evidence : [];
  if (!items.length) return '<span class="small muted">无证据</span>';
  return items.slice(0, 6).map((item) => {
    const color = item.type === 'test' ? '#1a7f37' : item.type === 'branch' ? '#8250df'
      : item.type === 'commit' ? '#9a6700' : '#0969da';
    const label = String(item.ref || item.type || '').slice(0, 42);
    return `<gh-label color="${color}">${esc(label)}</gh-label>`;
  }).join(' ');
}

function insightCard(insight, slug, canEdit) {
  const confidence = Math.round((Number(insight.confidence) || 0) * 100);
  const suggestion = insight.suggestion || {};
  const status = STATUS_TEXT[suggestion.status] || suggestion.status || '';
  const issueLink = insight.issue_number
    ? `<a class="small" href="/${esc(slug)}/issues/${esc(insight.issue_number)}">#${esc(insight.issue_number)}</a>`
    : '<span class="small muted">未关联议题</span>';
  // 标题里已经带了「#n 议题名」，就不要再重复一个编号链接
  const showIssueChip = insight.issue_number && !/^#\d+/.test(String(insight.title || ''));
  const tone = confidence >= 75 ? 'ok' : confidence >= 50 ? 'warn' : 'danger';
  const stateBadge = insight.state === 'draft'
    ? '<gh-badge state="accent" dot>待确认</gh-badge>'
    : insight.state === 'rejected'
      ? '<gh-badge state="closed" dot>已驳回</gh-badge>'
      : insight.state === 'confirmed'
        ? '<gh-badge state="open" dot>已应用</gh-badge>'
        : insight.state === 'reverted'
          ? '<gh-badge state="closed" dot>已撤销</gh-badge>'
          : `<gh-badge state="open" dot>${esc(insight.state)}</gh-badge>`;
  const proposal = isProposal(insight);
  const applyBtn = proposal && insight.state === 'draft' && canEdit
    ? `<gh-btn size="sm" variant="primary" data-act="apply" data-id="${esc(insight.id)}"><gh-icon name="check" size="12"></gh-icon> 预览并应用</gh-btn>`
    : '';
  const revertBtn = insight.state === 'confirmed' && canEdit
    ? `<gh-btn size="sm" variant="invisible" data-act="revert" data-id="${esc(insight.id)}"><gh-icon name="undo" size="12"></gh-icon> 撤销</gh-btn>`
    : '';
  return `<div class="insight-card${proposal ? ' is-proposal' : ''}">
    <div class="insight-card__head">
      <gh-icon name="sparkle" size="16"></gh-icon>
      <b class="grow">${esc(insight.title || '进度线索')}</b>
      ${proposal ? kindBadge(insight.kind) : ''}
      ${showIssueChip ? issueLink : ''}
      ${stateBadge}
    </div>
    <div class="insight-card__summary">${esc(insight.summary || '')}</div>
    ${proposal ? actionList(insight) : ''}
    <div class="insight-card__refs">${evidenceChips(insight.evidence)}</div>
    ${risksBlock(insight)}
    <div class="insight-card__foot">
      <span class="small muted">置信度 ${confidence}%</span>
      <gh-progress value="${confidence}" max="100" thin tone="${tone}" style="flex:1;max-width:160px"></gh-progress>
      ${suggestion.status ? `<span class="small muted">建议：${esc(status)}${suggestion.plan_week ? ` · ${esc(suggestion.plan_week)}` : ''}</span>` : ''}
      <span class="grow"></span>
      ${insight.state === 'draft' && canEdit
        ? `${applyBtn}<gh-btn size="sm" variant="invisible" data-act="reject" data-id="${esc(insight.id)}"><gh-icon name="x" size="12"></gh-icon> 驳回</gh-btn>`
        : revertBtn}
    </div>
    <div class="small muted">${esc(fmtDateTime(insight.created_at))} 产生${
      insight.events && insight.events.length > 1 ? ` · ${insight.events.length - 1} 次合并` : ''
    }${insight.decided_by ? ` · ${esc(insight.decided_by)} 处理` : ''}</div>
  </div>`;
}

function auditRow(entry) {
  return `<tr>
    <td class="mono small">${esc(entry.action)}</td>
    <td>${esc(entry.actor_name || '系统')}</td>
    <td class="small muted">${esc(entry.subject_type || '')}${entry.subject_id ? ` #${esc(entry.subject_id)}` : ''}</td>
    <td class="small muted">${esc(fmtDateTime(entry.created_at))}</td>
  </tr>`;
}

export function openPairDialog(slug, onDone = null) {
  return openDialog({
    title: '生成配对码',
    body: `<div class="dlg-note">生成后把配对码填到端侧 SDK 的配置里，设备第一次连上就自动激活。
      配对码只在这次显示，过期时间 60 分钟。</div>
      ${fieldHTML({ label: '设备名称（可留空）', name: 'name', placeholder: '例如：林薇的 MacBook', maxlength: 120 })}`,
    okText: '生成',
    cancelText: '取消',
    onSubmit: async (values, dialog) => {
      try {
        const data = await api.post(`/api/teams/${encodeURIComponent(slug)}/engine/edge/pair`, {
          name: String(values.name || '').trim(),
          platform: '',
        });
        dialog.close();
        openTokenDialog(data, onDone);
      } catch (error) {
        toast(error.message || '生成失败', 'err');
        return false;
      }
      return true;
    },
  });
}

export function openTokenDialog(data, onDone = null) {
  const token = data.token || '';
  const wsPath = `${location.origin.replace(/^http/, 'ws')}${data.ws_path || '/api/edge/ws'}`;
  const usage = `from core.network import EdgeClient\n\nclient = EdgeClient(\n    "${wsPath}",\n    "${token}",\n    device={"name": "我的电脑", "platform": "darwin"},\n)\nawait client.run_forever()`;
  openDialog({
    title: '配对码已生成',
    body: `<div class="dlg-note">只显示一次，请立即复制。设备吊销后需要重新生成。</div>
      <gh-field label="配对码"><gh-input value="${esc(token)}" readonly></gh-input></gh-field>
      <gh-field label="上报地址"><gh-input value="${esc(wsPath)}" readonly></gh-input></gh-field>
      <gh-field label="端侧用法"><gh-textarea rows="8" value="${esc(usage)}" readonly></gh-textarea></gh-field>`,
    okText: '完成',
    cancelText: '',
    onMount: (dialog) => {
      const copy = document.createElement('gh-btn');
      copy.setAttribute('size', 'sm');
      copy.setAttribute('data-act', 'copy-token');
      copy.innerHTML = '<gh-icon name="copy" size="12"></gh-icon> 复制配对码';
      dialog.addEventListener('click', async (event) => {
        if (!event.target.closest('[data-act="copy-token"]')) return;
        try {
          await navigator.clipboard.writeText(token);
          toast('配对码已复制');
        } catch {
          toast('复制失败，请手动选择配对码', 'err');
        }
      });
      const body = dialog.querySelector('.gh-dialog__body');
      if (body) body.appendChild(copy);
    },
    onSubmit: async () => {
      if (typeof onDone === 'function') onDone();
      return true;
    },
  });
}

export function openProposalDialog(slug, onDone = null) {
  const options = PROPOSAL_KINDS.map((kind) => {
    const meta = KIND_META[kind];
    return `<option value="${kind}">${esc(meta.label)}${meta.hint ? `（${esc(meta.hint)}）` : ''}</option>`;
  }).join('');
  return openDialog({
    title: '生成提案',
    body: `<div class="dlg-note">提案只是草稿：先预览动作，确认后才会真正建议题 / 写验收标准 / 指派负责人。所有动作都会进审计链，5 分钟内可撤销。</div>
      <gh-field label="提案类型" hint="拆分与任务树会按议题正文清单或代码事件证据生成">
        <gh-select name="kind">${options}</gh-select>
      </gh-field>
      <div class="form-grid">
        ${fieldHTML({ label: '议题编号（拆分/负责人建议用）', name: 'issue', placeholder: '例如：12', hint: '留空则按类型选择' })}
        ${fieldHTML({ label: '里程碑编号（任务树/验收标准用）', name: 'milestone', placeholder: '例如：1' })}
      </div>`,
    okText: '生成提案',
    cancelText: '取消',
    onSubmit: async (values) => {
      const payload = { kind: String(values.kind || 'split') };
      const issue = String(values.issue || '').trim().replace(/^#/, '');
      const milestone = String(values.milestone || '').trim().replace(/^#/, '');
      if (issue) payload.issue = Number(issue);
      if (milestone) payload.milestone = Number(milestone);
      try {
        const result = await api.post(`/api/teams/${encodeURIComponent(slug)}/engine/proposals`, payload);
        if (result.created) toast('提案已生成，去收件箱确认');
        else toast(result.message || '当前没有可用的提案依据', 'err');
        if (typeof onDone === 'function') await onDone();
        return true;
      } catch (error) {
        toast(error.message || '生成失败', 'err');
        return false;
      }
    },
  });
}

export function openApplyDialog(slug, result, onDone = null) {
  const preview = Array.isArray(result.preview) ? result.preview : [];
  const rows = preview.length
    ? preview.map((item) => `<div class="row" style="gap:8px;align-items:baseline;padding:6px 0;border-bottom:1px solid var(--gh-border-muted)">
        <gh-badge state="open">${esc(OP_TEXT[item.op] || item.op)}</gh-badge>
        <span class="grow small">${esc(item.text || '')}</span>
      </div>`).join('')
    : '<div class="small muted">没有可执行的动作。</div>';
  return openDialog({
    title: '预览提案动作',
    body: `<div class="dlg-note">下面是确认后会执行的动作，先看一遍再点「确认执行」。执行后 5 分钟内可撤销。</div>${rows}`,
    okText: '确认执行',
    cancelText: '再想想',
    onSubmit: async () => {
      try {
        const applied = await api.post(`/api/teams/${encodeURIComponent(slug)}/engine/insights/${encodeURIComponent(result.insight_id)}/apply`, { dry_run: false });
        const created = applied.applied || 0;
        toast(`已应用 ${created} 个动作`);
        if (typeof onDone === 'function') await onDone();
        return true;
      } catch (error) {
        toast(error.message || '执行失败', 'err');
        return false;
      }
    },
  });
}

export function openQueryDialog(slug, device, onDone = null) {
  return openDialog({
    title: `反向查询 · ${device.name || '设备'}`,
    width: 'wide',
    body: `<div class="dlg-note">端侧只会在自己的仓库根目录里回答。一行一个相对路径（最多 20 个）；
      「元信息」模式只回大小/摘要/行数，「读取内容」模式会带回脱敏后的文本，单文件最多 8 KB。</div>
      <gh-field label="文件（一行一个）" hint="例如：client/core/query.py">
        <gh-textarea name="files" rows="5" placeholder="server/app/main.py&#10;client/core/network.py"></gh-textarea>
      </gh-field>
      <div class="form-grid">
        ${fieldHTML({
          label: '查询模式',
          name: 'mode',
          type: 'select',
          value: 'stat',
          options: [
            { value: 'stat', label: '元信息（大小 / 摘要 / 行数）' },
            { value: 'read', label: '读取内容（已脱敏）' },
          ],
        })}
        ${fieldHTML({ label: '单文件上限（字节）', name: 'max_bytes', value: '8192' })}
      </div>`,
    okText: '开始查询',
    cancelText: '取消',
    onSubmit: async (values) => {
      const files = String(values.files || '')
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter(Boolean);
      if (!files.length) {
        toast('请至少填一个文件路径', 'err');
        return false;
      }
      try {
        const result = await api.post(
          `/api/teams/${encodeURIComponent(slug)}/engine/edge/devices/${encodeURIComponent(device.id)}/query`,
          {
            files,
            mode: String(values.mode || 'stat'),
            max_bytes: Number(values.max_bytes || 8192) || 8192,
            reason: '引擎页手动查询',
          },
        );
        if (typeof onDone === 'function') await onDone();
        showQueryResult(result);
        return true;
      } catch (error) {
        toast(error.message || '查询失败', 'err');
        return false;
      }
    },
  });
}

export function showQueryResult(result) {
  const files = result.files || {};
  const names = Object.keys(files);
  const rows = names.length
    ? names.map((name) => {
      const info = files[name] || {};
      const meta = info.ok === false
        ? `<gh-badge state="danger">失败</gh-badge> <span class="small muted">${esc(info.error || '')}</span>`
        : `<span class="small muted">${esc(String(info.size ?? '—'))} 字节 · ${info.lines == null ? '行数未知' : `${esc(info.lines)} 行`} · ${esc(info.lang || '')}${info.truncated ? ' · 已截断' : ''}${info.redactions && Object.keys(info.redactions).length ? ' · 已脱敏' : ''}</span>`;
      const content = info.content
        ? `<pre class="query-result__code">${esc(String(info.content).slice(0, 4000))}</pre>`
        : '';
      return `<div class="query-result">
        <div class="row" style="gap:8px;align-items:baseline">
          <gh-icon name="file" size="14"></gh-icon>
          <b class="mono small grow">${esc(name)}</b>
          <span class="mono small muted" title="${esc(info.sha256 || '')}">${esc(String(info.sha256 || '').slice(0, 12))}</span>
        </div>
        <div>${meta}</div>
        ${content}
      </div>`;
    }).join('')
    : '<div class="small muted">端侧没有回答任何文件。</div>';
  const error = result.error
    ? `<gh-alert type="warning" dismissible>${esc(result.error)}</gh-alert>`
    : '';
  return openDialog({
    title: `查询结果（${names.length} 个文件）`,
    width: 'wide',
    body: `${error}<div class="dlg-note">设备：${esc(result.device_name || result.device_id || '')} · 模式：${esc(result.mode || 'stat')}</div>${rows}`,
    okText: null,
    cancelText: null,
  });
}

export async function render(view, route) {
  const slug = S.detail.slug;
  let overview;
  let insights;
  let audits;
  let applied;
  try {
    [overview, insights, audits, applied] = await Promise.all([
      api.get(`/api/teams/${encodeURIComponent(slug)}/engine/edge`),
      api.get(`/api/teams/${encodeURIComponent(slug)}/engine/insights`, { state: 'draft' }),
      api.get(`/api/teams/${encodeURIComponent(slug)}/engine/audit`, { limit: 10 }),
      api.get(`/api/teams/${encodeURIComponent(slug)}/engine/insights`, { state: 'confirmed', limit: 5 }),
    ]);
  } catch (error) {
    view.innerHTML = `<div class="error-box"><gh-icon name="alert" size="24"></gh-icon>
      <div class="error-box__title">推理引擎加载失败</div>
      <div class="error-box__msg">${esc(error.message || '')}</div>
      <gh-btn size="sm" data-act="retry">重试</gh-btn></div>`;
    onView(view, 'click', (event) => {
      if (event.target.closest('[data-act="retry"]')) render(view, route);
    });
    return;
  }

  const canManage = !!overview.can_manage || S.canEdit;
  const devices = Array.isArray(overview.devices) ? overview.devices : [];
  const online = devices.filter((item) => item.online).length;
  const events = Array.isArray(overview.events) ? overview.events : [];
  const jobs = Array.isArray(overview.jobs) ? overview.jobs : [];
  const drafts = Array.isArray(insights.insights) ? insights.insights : [];
  const recent = Array.isArray(applied && applied.insights) ? applied.insights : [];
  const entries = Array.isArray(audits.entries) ? audits.entries : [];
  const pendingJobs = Number(overview.pending_jobs || 0);
  const worker = overview.worker || {};
  const pausedIds = new Set((overview.paused || []).map((id) => String(id)));
  const queued = jobs.filter((job) => job.status === 'queued' || job.status === 'running').length;

  const deviceRows = devices.length
    ? devices.map((device) => deviceRow(device, canManage, pausedIds.has(String(device.id)))).join('')
    : `<tr><td colspan="5">${emptyHTML({ icon: 'desktop-download', title: '还没有端侧设备', desc: canManage ? '点右上角「生成配对码」，把配对码填进端侧 SDK。' : '该团队还没有接入端侧探针。' })}</td></tr>`;

  const eventRows = events.length
    ? events.slice(0, 8).map((event) => `<tr>
        <td><div class="mono small">${esc(event.event_uid)}</div><div class="small muted">${esc(event.branch || '无分支')}</div></td>
        <td class="small">${esc(event.kind)}</td>
        <td class="mono small">${esc(event.file_count || 0)} 文件</td>
        <td class="small muted">${esc(timeAgo(event.created_at))}</td>
      </tr>`).join('')
    : '';

  view.innerHTML = `${pageHead('推理引擎', '端侧脱敏上报代码变更 → 规则推断进度 → 生成规划提案 → 人工确认后落库（P1 推断 + P2.5 层级与提案）', `
      <gh-btn size="sm" data-act="refresh"><gh-icon name="sync" size="14"></gh-icon> 刷新</gh-btn>
      ${canManage ? '<gh-btn size="sm" data-act="drain"><gh-icon name="zap" size="14"></gh-icon> 立即推断</gh-btn>' : ''}
      ${canManage ? '<gh-btn size="sm" data-act="proposal"><gh-icon name="tasklist" size="14"></gh-icon> 生成提案</gh-btn>' : ''}
      ${canManage ? '<gh-btn size="sm" variant="primary" data-act="pair"><gh-icon name="plus" size="14"></gh-icon> 生成配对码</gh-btn>' : ''}
    `)}
    <gh-alert type="note">
      引擎${worker.running ? '正在后台运行' : '已按配置关闭后台循环'}：已处理 ${esc(worker.done || 0)} 个作业，产生 ${esc(worker.insights || 0)} 条洞察。
      端侧只上报脱敏后的文件元信息（路径 / 增删行数 / 是否涉及测试），不传代码内容。
    </gh-alert>
    <div class="grid-stats">
      <gh-stat label="在线设备" value="${esc(online)}" unit="/ ${esc(devices.length)}" icon="desktop-download"></gh-stat>
      <gh-stat label="待确认洞察" value="${esc(drafts.length)}" icon="sparkle" tone="accent"></gh-stat>
      <gh-stat label="排队作业" value="${esc(pendingJobs || queued)}" icon="workflow"></gh-stat>
      <gh-stat label="累计事件" value="${esc(events.length)}" icon="git-commit" tone="purple"></gh-stat>
    </div>
    <gh-box title="进度洞察与提案（待确认）">
      ${drafts.length ? drafts.map((insight) => insightCard(insight, slug, canManage)).join('')
        : emptyHTML({ icon: 'sparkle', title: '暂无待确认的洞察', desc: '端侧上报代码变更后，引擎会自动推断进度；也可以点右上角「生成提案」按议题或里程碑生成拆分/任务树草稿。' })}
    </gh-box>
    ${recent.length ? `<gh-box title="最近已应用（5 分钟内可撤销）">
      ${recent.map((insight) => insightCard(insight, slug, canManage)).join('')}
    </gh-box>` : ''}
    <div class="split-2-even">
      <gh-box title="端侧设备 (${devices.length})">
        <gh-table dense><table><thead><tr>
          <th>设备</th><th>状态</th><th>事件</th><th>最近活跃</th><th></th>
        </tr></thead><tbody>${deviceRows}</tbody></table></gh-table>
      </gh-box>
      <gh-box title="最近审计（哈希链）">
        ${entries.length ? `<gh-table dense><table><thead><tr>
            <th>动作</th><th>操作者</th><th>对象</th><th>时间</th>
          </tr></thead><tbody>${entries.map(auditRow).join('')}</tbody></table></gh-table>
          <div class="row" style="margin-top:10px">
            <span class="small muted grow">每行带 prev_hash，可用「校验」确认没有被改过。</span>
            <gh-btn size="sm" variant="invisible" data-act="verify"><gh-icon name="shield-check" size="12"></gh-icon> 校验</gh-btn>
          </div>`
        : emptyHTML({ icon: 'shield', title: '还没有审计记录', desc: '配对设备、产生或驳回洞察都会留痕。' })}
      </gh-box>
    </div>
    ${eventRows ? `<gh-box title="最近事件">
      <gh-table dense><table><thead><tr>
        <th>事件</th><th>类型</th><th>规模</th><th>时间</th>
      </tr></thead><tbody>${eventRows}</tbody></table></gh-table>
    </gh-box>` : ''}`;

  onView(view, 'click', async (event) => {
    const act = event.target.closest('[data-act]');
    if (!act) return;
    const name = act.getAttribute('data-act');
    try {
      if (name === 'refresh' || name === 'retry') { render(view, route); return; }
      if (name === 'pair') { openPairDialog(slug, () => render(view, route)); return; }
      if (name === 'proposal') { openProposalDialog(slug, () => render(view, route)); return; }
      if (name === 'apply') {
        const id = act.getAttribute('data-id');
        const preview = await api.post(`/api/teams/${encodeURIComponent(slug)}/engine/insights/${encodeURIComponent(id)}/apply`, { dry_run: true });
        openApplyDialog(slug, { insight_id: id, preview: preview.preview || [] }, () => render(view, route));
        return;
      }
      if (name === 'revert') {
        const id = act.getAttribute('data-id');
        const ok = await confirmAction({
          title: '撤销这次应用', message: '撤销会删除本次创建的议题/里程碑，并把改过的字段恢复原值。仅在应用后 5 分钟内可撤销。', okText: '撤销', danger: true,
        });
        if (!ok) return;
        const result = await api.post(`/api/teams/${encodeURIComponent(slug)}/engine/insights/${encodeURIComponent(id)}/revert`, {});
        toast(`已撤销：${(result.removed || []).length + (result.restored || []).length} 项`);
        render(view, route);
        return;
      }
      if (name === 'drain') {
        const result = await api.post(`/api/teams/${encodeURIComponent(slug)}/engine/jobs/drain?limit=10`);
        toast(`已处理 ${result.processed || 0} 个作业`);
        render(view, route);
        return;
      }
      if (name === 'verify') {
        const result = await api.get(`/api/teams/${encodeURIComponent(slug)}/engine/audit/verify`);
        if (result.ok) toast(`审计链完整：校验了 ${result.checked} 条记录`);
        else toast(`审计链在 #${result.broken_at && result.broken_at.id} 处断裂`, 'err');
        return;
      }
      if (name === 'pause' || name === 'resume') {
        const id = act.getAttribute('data-id');
        const paused = name === 'pause';
        await api.post(`/api/teams/${encodeURIComponent(slug)}/engine/edge/devices/${id}/pause`, { paused });
        toast(paused ? '已暂停该设备的上报' : '已恢复该设备的上报');
        render(view, route);
        return;
      }
      if (name === 'query') {
        const id = act.getAttribute('data-id');
        const device = (overview.devices || []).find((item) => String(item.id) === String(id));
        if (!device) {
          toast('设备不存在或已下线', 'err');
          return;
        }
        openQueryDialog(slug, device, () => render(view, route));
        return;
      }
      if (name === 'revoke') {
        const id = act.getAttribute('data-id');
        const ok = await confirmAction({
          title: '吊销设备', message: '吊销后该设备需要重新生成配对码才能接入。', okText: '吊销', danger: true,
        });
        if (!ok) return;
        await api.del(`/api/teams/${encodeURIComponent(slug)}/engine/edge/devices/${id}`);
        toast('设备已吊销');
        render(view, route);
        return;
      }
      if (name === 'reject') {
        const id = act.getAttribute('data-id');
        const ok = await confirmAction({
          title: '驳回洞察', message: '驳回后该条线索不再出现在收件箱（审计里会留痕）。', okText: '驳回', danger: true,
        });
        if (!ok) return;
        await api.post(`/api/teams/${encodeURIComponent(slug)}/engine/insights/${id}/reject`, { reason: '人工驳回' });
        toast('已驳回');
        render(view, route);
      }
    } catch (error) {
      toast(error.message || '操作失败', 'err');
    }
  });
}
