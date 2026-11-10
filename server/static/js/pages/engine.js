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
  const actions = canManage && device.state !== 'revoked'
    ? `${pauseBtn}
       <gh-btn size="sm" variant="invisible" data-act="revoke" data-id="${esc(device.id)}">
         <gh-icon name="x" size="12"></gh-icon> 吊销
       </gh-btn>`
    : '';
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
      : `<gh-badge state="open" dot>${esc(insight.state)}</gh-badge>`;
  return `<div class="insight-card">
    <div class="insight-card__head">
      <gh-icon name="sparkle" size="16"></gh-icon>
      <b class="grow">${esc(insight.title || '进度线索')}</b>
      ${showIssueChip ? issueLink : ''}
      ${stateBadge}
    </div>
    <div class="insight-card__summary">${esc(insight.summary || '')}</div>
    <div class="insight-card__refs">${evidenceChips(insight.evidence)}</div>
    <div class="insight-card__foot">
      <span class="small muted">置信度 ${confidence}%</span>
      <gh-progress value="${confidence}" max="100" thin tone="${tone}" style="flex:1;max-width:160px"></gh-progress>
      ${suggestion.status ? `<span class="small muted">建议：${esc(status)}${suggestion.plan_week ? ` · ${esc(suggestion.plan_week)}` : ''}</span>` : ''}
      <span class="grow"></span>
      ${insight.state === 'draft' && canEdit
        ? `<gh-btn size="sm" variant="invisible" data-act="reject" data-id="${esc(insight.id)}"><gh-icon name="x" size="12"></gh-icon> 驳回</gh-btn>`
        : ''}
    </div>
    <div class="small muted">${esc(fmtDateTime(insight.created_at))} 产生${
      insight.events && insight.events.length > 1 ? ` · ${insight.events.length - 1} 次合并` : ''
    }</div>
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

export async function render(view, route) {
  const slug = S.detail.slug;
  let overview;
  let insights;
  let audits;
  try {
    [overview, insights, audits] = await Promise.all([
      api.get(`/api/teams/${encodeURIComponent(slug)}/engine/edge`),
      api.get(`/api/teams/${encodeURIComponent(slug)}/engine/insights`, { state: 'draft' }),
      api.get(`/api/teams/${encodeURIComponent(slug)}/engine/audit`, { limit: 10 }),
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

  view.innerHTML = `${pageHead('推理引擎', '端侧脱敏上报代码变更 → 规则推断进度 → 人工确认后写回（P1：推断与收件箱，写回在 P2）', `
      <gh-btn size="sm" data-act="refresh"><gh-icon name="sync" size="14"></gh-icon> 刷新</gh-btn>
      ${canManage ? '<gh-btn size="sm" data-act="drain"><gh-icon name="zap" size="14"></gh-icon> 立即推断</gh-btn>' : ''}
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
    <gh-box title="进度洞察（待确认）">
      ${drafts.length ? drafts.map((insight) => insightCard(insight, slug, canManage)).join('')
        : emptyHTML({ icon: 'sparkle', title: '暂无待确认的洞察', desc: '端侧上报代码变更后，引擎会自动推断进度并在这里出卡片。' })}
    </gh-box>
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
