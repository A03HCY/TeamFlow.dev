/* TeamFlow · 权限（客户端采集策略）
 *
 * 管理员在这里控制端侧探针的行为：
 *   1. 无操作间隔：文件夹连续多久没有写入才认为「这一轮干完了」，默认 3 分钟；
 *   2. 是否允许客户端查看变动内容与进度，默认关闭（静默分析上传）。
 * 端侧每次上报前都会拉一次策略，改完立刻生效，不需要重新配对。
 */

import { api } from '../api.js';
import { S } from '../store.js';
import { esc, pageHead, toast, onView, emptyHTML, errorHTML, timeAgo } from '../ui.js';

const STATUS_TEXT = {
  received: '已接收',
  ingested: '已分析',
  rejected: '已拒收',
  ingest_failed: '分析失败',
};

const MIN_IDLE = 1;
const MAX_IDLE = 1440;

function policyForm(policy) {
  const readOnly = !policy.can_admin;
  return `<gh-box title="采集策略">
    <gh-field label="无操作间隔（分钟）"
      hint="文件夹连续这么多分钟没有写入，客户端才认为这一轮工作结束，把攒下的改动生成日志并上传。默认 3 分钟；演示时想立刻看结果，客户端可点「立即分析」。">
      <gh-input name="idle_minutes" type="number" min="${MIN_IDLE}" max="${MAX_IDLE}"
        value="${esc(policy.idle_minutes ?? 3)}"${readOnly ? ' readonly' : ''}></gh-input>
    </gh-field>

    <gh-field label="允许客户端查看变动内容与进度"
      hint="关闭时客户端只上传统计结果与符号名（新增了哪些函数/类），界面上不展示文件路径（静默分析）；开启后会连文件路径一起上传，便于当面排查。">
      <gh-switch name="allow_view" status status-on="可见" status-off="静默"
        ${policy.allow_view ? 'checked' : ''}${readOnly ? ' disabled' : ''}></gh-switch>
    </gh-field>

    <div class="row" style="gap:8px;padding-top:4px">
      <gh-btn variant="primary" data-act="save"${readOnly ? ' disabled' : ''}>保存策略</gh-btn>
      <span class="small muted" data-role="stamp">
        ${policy.updated_at ? `上次修改：${esc(timeAgo(policy.updated_at))}` : '尚未修改过'}
      </span>
    </div>
  </gh-box>`;
}

function devicesBox(devices) {
  if (!devices.length) {
    return `<gh-box title="接入的客户端">
      ${emptyHTML({ icon: 'server', title: '还没有客户端接入', desc: '在「引擎」页生成配对码，然后在客户端执行 set 命令。' })}
    </gh-box>`;
  }
  const rows = devices.map((device) => `<tr>
    <td>${esc(device.name || '未命名设备')}</td>
    <td class="mono">${esc(device.platform || '—')}</td>
    <td>${
      device.revoked_at
        ? '<gh-label tone="danger">已吊销</gh-label>'
        : device.online
          ? '<gh-label tone="success">在线</gh-label>'
          : '<gh-label>离线</gh-label>'
    }</td>
    <td class="mono">${Number(device.event_count || 0)}</td>
    <td class="small muted">${device.last_seen_at ? esc(timeAgo(device.last_seen_at)) : '—'}</td>
  </tr>`).join('');
  return `<gh-box title="接入的客户端">
    <gh-table dense>
      <table>
        <thead><tr><th>设备</th><th>平台</th><th>状态</th><th>事件</th><th>最近心跳</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </gh-table>
  </gh-box>`;
}

function logsInner(logs) {
  if (!logs.length) {
    return emptyHTML({ icon: 'history', title: '还没有日志', desc: '客户端等到一个无人操作窗口后才会生成第一条。' });
  }
  const rows = logs.map((log) => {
    const text = String(log.analysis || log.summary || '').replace(/\s+/g, ' ').trim();
    const short = text.length > 64 ? `${text.slice(0, 64)}…` : text;
    return `<tr>
      <td class="small muted">${esc(timeAgo(log.created_at))}</td>
      <td class="mono">${esc(log.repo_label || '—')}</td>
      <td class="mono">${Number(log.file_count || 0)}</td>
      <td class="mono">+${Number(log.additions || 0)}/−${Number(log.deletions || 0)}</td>
      <td title="${esc(text)}">${esc(short || '（静默模式，无明细）')}</td>
      <td><gh-label>${esc(STATUS_TEXT[log.status] || log.status || '')}</gh-label></td>
    </tr>`;
  }).join('');
  return `<gh-table dense>
    <table>
      <thead><tr><th>时间</th><th>仓库</th><th>文件</th><th>行数</th><th>分析</th><th>状态</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  </gh-table>`;
}

function latestInner(logs) {
  const newest = logs && logs[0];
  if (!newest) {
    return '<span class="small muted">还没有分析结果，等客户端上报第一条日志。</span>';
  }
  const text = String(newest.analysis || '').trim() || String(newest.summary || '').trim();
  const stamp = `<div class="small muted" style="margin-bottom:6px">
    ${esc(newest.repo_label || '—')} · ${esc(newest.branch || '无分支')} ·
    ${Number(newest.file_count || 0)} 个文件 +${Number(newest.additions || 0)}/−${Number(newest.deletions || 0)} ·
    ${esc(timeAgo(newest.created_at))}
  </div>`;
  return stamp + `<div style="white-space:pre-wrap;line-height:1.75">${esc(text || '（这条日志没有分析文本）')}</div>`;
}

function logsBox(logs) {
  return `<gh-box title="最近日志">
    <div class="row" style="margin-bottom:8px">
      <span class="small muted grow" data-role="sync">自动同步已开启（每 6 秒）</span>
    </div>
    <div data-role="logs">${logsInner(logs)}</div>
  </gh-box>
  <gh-box title="最近一次分析（与客户端同步）">
    <div data-role="latest">${latestInner(logs)}</div>
  </gh-box>`;
}

export async function render(view, route) {
  const slug = S.detail.slug;
  let data;
  let logs = [];
  try {
    data = await api.get(`/api/teams/${slug}/client-policy`);
    if (data.policy && data.policy.can_admin) {
      const res = await api.get(`/api/teams/${slug}/client-logs`, { limit: 10 });
      logs = res.logs || [];
    }
  } catch (error) {
    view.innerHTML = errorHTML(error.message || '权限页加载失败');
    onView(view, 'click', (event) => {
      if (event.target.closest('[data-act="retry"]')) render(view, route);
    });
    return;
  }

  const policy = data.policy || {};
  const devices = data.devices || [];

  view.innerHTML = `${pageHead(
    '权限',
    '客户端（端侧探针）的采集策略：多久算一轮、能不能看见细节',
    '<gh-btn size="sm" data-act="refresh"><gh-icon name="sync" size="14"></gh-icon> 刷新</gh-btn>',
  )}
  ${policy.can_admin ? '' : `<gh-callout tone="warning" title="只读">
      只有团队管理员可以修改客户端策略，当前身份仅可查看。
    </gh-callout>`}
  ${policyForm(policy)}
  ${devicesBox(devices)}
  ${policy.can_admin ? logsBox(logs) : ''}`;

  /* ---------- 与客户端保持同步：轮询日志，只替换两块内容，不重绘整页 ---------- */
  let lastNewest = Number((logs[0] && logs[0].id) || 0);
  let timer = null;
  const clock = () => new Date().toLocaleTimeString('zh-CN', { hour12: false });
  const stopSync = () => { if (timer) { clearInterval(timer); timer = null; } };

  async function syncLogs(announce) {
    if (!view.isConnected) { stopSync(); return; }
    let next;
    try {
      const res = await api.get(`/api/teams/${slug}/client-logs`, { limit: 10 });
      next = res.logs || [];
    } catch (error) {
      const stamp = view.querySelector('[data-role="sync"]');
      if (stamp) stamp.textContent = '自动同步中断，点右上角「刷新」重试';
      return;
    }
    const slot = view.querySelector('[data-role="logs"]');
    const latest = view.querySelector('[data-role="latest"]');
    if (slot) slot.innerHTML = logsInner(next);
    if (latest) latest.innerHTML = latestInner(next);
    const newest = Number((next[0] && next[0].id) || 0);
    const fresh = newest > lastNewest && lastNewest > 0;
    const stamp = view.querySelector('[data-role="sync"]');
    if (stamp) {
      stamp.textContent = fresh
        ? `已同步 · ${clock()} · 新增 ${newest - lastNewest} 条`
        : `自动同步 · 更新于 ${clock()}`;
    }
    if (fresh && announce) toast('客户端有新日志同步进来');
    lastNewest = newest || lastNewest;
  }

  if (policy.can_admin) timer = setInterval(() => syncLogs(true), 6000);

  onView(view, 'click', async (event) => {
    const act = event.target.closest('[data-act]');
    if (!act) return;
    const name = act.getAttribute('data-act');
    if (name === 'refresh') {
      render(view, route);
      return;
    }
    if (name !== 'save') return;

    const input = view.querySelector('gh-input[name="idle_minutes"]');
    const toggle = view.querySelector('gh-switch[name="allow_view"]');
    const idle = Number(input?.value ?? policy.idle_minutes);
    if (!Number.isFinite(idle) || idle < MIN_IDLE || idle > MAX_IDLE) {
      toast(`间隔需在 ${MIN_IDLE}～${MAX_IDLE} 分钟之间`, 'err');
      return;
    }
    const box = toggle?.querySelector('input');
    const allow = box ? Boolean(box.checked) : Boolean(toggle?.hasAttribute('checked'));

    act.setAttribute('disabled', '');
    try {
      const res = await api.put(`/api/teams/${slug}/client-policy`, {
        idle_minutes: idle,
        allow_view: allow,
      });
      const next = res.policy || {};
      policy.idle_minutes = next.idle_minutes;
      policy.allow_view = next.allow_view;
      policy.updated_at = next.updated_at;
      const stamp = view.querySelector('[data-role="stamp"]');
      if (stamp) stamp.textContent = '刚刚已保存';
      toast(`已保存：${next.idle_minutes} 分钟 · ${next.allow_view ? '可见' : '静默'}`);
    } catch (error) {
      toast(error.message || '保存失败', 'err');
    } finally {
      act.removeAttribute('disabled');
    }
  });
}
