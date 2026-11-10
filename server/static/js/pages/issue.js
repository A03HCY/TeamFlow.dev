/* TeamFlow · 议题详情 */

import { api } from '../api.js';
import { S, invalidateTeam } from '../store.js';
import { navigate } from '../router.js';
import {
  esc, avatarHTML, statusBadge, priorityBadge, stateBadge, labelChips, fmtDate,
  fmtWeek, fmtDateTime, timeAgo, emptyHTML, toast, confirmAction, errorHTML,
  onView,
} from '../ui.js';
import { openIssueDialog, deleteIssue } from '../issue-form.js';

function attrRow(label, html) {
  return `<div class="attr"><div class="attr__label">${esc(label)}</div><div class="attr__value">${html || '<span class="muted">—</span>'}</div></div>`;
}

export async function render(view, route) {
  const slug = S.detail.slug;
  const number = route.arg;
  if (!number) { navigate(`/${slug}/issues`, { replace: true }); return; }

  let data;
  try {
    data = await api.get(`/api/teams/${encodeURIComponent(slug)}/issues/${encodeURIComponent(number)}`);
  } catch (error) {
    view.innerHTML = errorHTML(error.message || '议题不存在', `/${slug}/issues/${number}`);
    onView(view, 'click', (event) => {
      if (event.target.closest('[data-act="retry"]')) render(view, route);
    });
    return;
  }

  const issue = data.issue || {};
  const comments = Array.isArray(data.comments) ? data.comments : [];
  const canEdit = !!data.can_edit || S.canEdit;
  const closed = issue.state === 'closed';
  const assignee = issue.assignee_id
    ? { display_name: issue.assignee_name, avatar_url: issue.assignee_avatar_url }
    : null;

  document.title = `#${issue.number} ${issue.title} · TeamFlow`;

  view.innerHTML = `
    <div class="row" style="gap:8px">
      <a class="small muted row" style="gap:2px" href="/${esc(slug)}/issues"><gh-icon name="chevron-left" size="14"></gh-icon> 返回议题列表</a>
    </div>

    <gh-page-head title="#${esc(issue.number)} ${esc(issue.title)}">
      <gh-btn size="sm" data-act="refresh"><gh-icon name="sync" size="14"></gh-icon> 刷新</gh-btn>
      ${canEdit ? `<gh-btn size="sm" data-act="toggle-state">${closed ? '重新打开' : '关闭议题'}</gh-btn>
        <gh-btn size="sm" data-act="edit"><gh-icon name="pencil" size="14"></gh-icon> 编辑</gh-btn>
        <gh-btn size="sm" variant="danger" data-act="delete"><gh-icon name="trash" size="14"></gh-icon> 删除</gh-btn>` : ''}
    </gh-page-head>

    <div class="detail">
      <div class="col" style="gap:16px">
        <gh-box>
          <div class="row" style="margin-bottom:8px">
            ${stateBadge(issue.state)}
            ${statusBadge(issue.status)}
            ${priorityBadge(issue.priority)}
            ${issue.labels && issue.labels.length ? `<span class="grow"></span>${labelChips(issue.labels)}` : ''}
          </div>
          <div id="issue-body" class="issue-body"></div>
        </gh-box>

        <gh-box title="评论 (${comments.length})">
          <div id="comment-list">
            ${comments.length ? comments.map((comment) => `<div class="comment">
              <div class="comment__head">
                ${avatarHTML(comment, 22)}
                <b>${esc(comment.author_name || '访客')}</b>
                <span class="muted small">评论于 ${esc(fmtDateTime(comment.created_at))}</span>
                ${comment.user_id ? `<span class="grow"></span><span class="small muted mono">@${esc(comment.username || '')}</span>` : ''}
              </div>
              <div class="comment__body" data-comment="${esc(comment.id)}"></div>
            </div>`).join('') : emptyHTML({ icon: 'comment', title: '还没有评论', desc: '在下方写下第一条评论。' })}
          </div>
          ${canEdit ? `<div class="composer">
            <gh-textarea name="comment" rows="3" placeholder="${esc(S.user ? `以 ${S.user.display_name || S.user.username} 的身份评论…` : '以访客身份评论…')}"></gh-textarea>
            <div class="row" style="justify-content:flex-end">
              <gh-btn size="sm" variant="primary" data-act="comment">发表评论</gh-btn>
            </div>
          </div>` : ''}
        </gh-box>
      </div>

      <aside class="detail__side">
        <gh-box title="属性">
          ${attrRow('状态', `${statusBadge(issue.status)} ${stateBadge(issue.state)}`)}
          ${attrRow('优先级', priorityBadge(issue.priority))}
          ${attrRow('负责人', assignee ? `<div class="row-tight">${avatarHTML(assignee, 20)}<span>${esc(assignee.display_name || '')}</span></div>` : '')}
          ${attrRow('里程碑', issue.milestone_title ? esc(issue.milestone_title) : '')}
          ${attrRow('计划周', issue.plan_week ? esc(fmtWeek(issue.plan_week)) : '')}
          ${attrRow('截止日期', issue.due_date ? esc(fmtDate(issue.due_date)) : '')}
          ${attrRow('标签', labelChips(issue.labels))}
          ${attrRow('作者', issue.author_name ? `<div class="row-tight">${avatarHTML({ display_name: issue.author_name, avatar_url: issue.author_avatar_url }, 20)}<span>${esc(issue.author_name)}</span></div>` : '')}
          ${attrRow('创建时间', esc(fmtDateTime(issue.created_at)))}
          ${attrRow('更新时间', esc(fmtDateTime(issue.updated_at)))}
          ${issue.closed_at ? attrRow('关闭时间', esc(fmtDateTime(issue.closed_at))) : ''}
        </gh-box>
      </aside>
    </div>`;

  /* Markdown 正文与评论使用 gh-md（内容放在 textContent 中） */
  const bodyHost = view.querySelector('#issue-body');
  if (bodyHost) {
    if (issue.body) {
      const md = document.createElement('gh-md');
      md.textContent = issue.body;
      bodyHost.append(md);
    } else {
      bodyHost.innerHTML = '<p class="muted">暂无描述。</p>';
    }
  }
  comments.forEach((comment) => {
    const host = view.querySelector(`[data-comment="${comment.id}"]`);
    if (!host) return;
    const md = document.createElement('gh-md');
    md.textContent = comment.body || '';
    host.append(md);
  });

  const openEdit = () => openIssueDialog({
    slug,
    issue,
    onSaved: () => { invalidateTeam(slug); render(view, route); },
  });

  onView(view, 'click', async (event) => {
    const act = event.target.closest('[data-act]');
    if (!act) return;
    const name = act.getAttribute('data-act');
    try {
      if (name === 'refresh') { render(view, route); return; }
      if (name === 'edit') { openEdit(); return; }
      if (name === 'toggle-state') {
        const next = closed ? 'open' : 'closed';
        const ok = await confirmAction({
          title: next === 'closed' ? '关闭议题' : '重新打开议题',
          message: next === 'closed' ? `确定关闭议题 #${issue.number} 吗？` : `确定重新打开议题 #${issue.number} 吗？`,
          okText: next === 'closed' ? '关闭' : '重新打开',
        });
        if (!ok) return;
        await api.patch(`/api/teams/${encodeURIComponent(slug)}/issues/${issue.number}`, { state: next });
        toast(next === 'closed' ? '议题已关闭' : '议题已重新打开');
        invalidateTeam(slug);
        render(view, route);
        return;
      }
      if (name === 'delete') {
        const done = await deleteIssue(slug, issue, () => navigate(`/${slug}/issues`));
        if (done) invalidateTeam(slug);
        return;
      }
      if (name === 'comment') {
        const field = view.querySelector('[name="comment"]');
        const text = String(field ? field.value : '').trim();
        if (!text) { toast('请输入评论内容', 'err'); return; }
        await api.post(`/api/teams/${encodeURIComponent(slug)}/issues/${issue.number}/comments`, { body: text });
        toast('评论已发布');
        render(view, route, { keepScroll: true });
        return;
      }
    } catch (error) {
      toast(error.message || '操作失败', 'err');
    }
  });

  onView(view, 'keydown', (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
      const field = event.target.closest('[name="comment"]');
      if (field) {
        event.preventDefault();
        view.querySelector('[data-act="comment"]')?.click();
      }
    }
  });

  const commentField = view.querySelector('gh-textarea[name="comment"]');
  if (commentField) {
    const inner = commentField.querySelector('textarea');
    if (inner) inner.addEventListener('keydown', (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
        event.preventDefault();
        view.querySelector('[data-act="comment"]')?.click();
      }
    });
  }
}
