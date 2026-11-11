/* TeamFlow · 议题详情 */

import { api } from '../api.js';
import { S, invalidateTeam } from '../store.js';
import { navigate } from '../router.js';
import {
  esc, avatarHTML, statusBadge, priorityBadge, stateBadge, labelChips, fmtDate,
  fmtWeek, fmtDateTime, emptyHTML, toast, confirmAction, errorHTML, onView,
} from '../ui.js';
import { openIssueDialog, deleteIssue } from '../issue-form.js';

function attrRow(label, html) {
  return `<div class="attr"><div class="attr__label">${esc(label)}</div><div class="attr__value">${html || '<span class="muted">—</span>'}</div></div>`;
}

/**
 * 评论时间线：用 gh-timeline 的富内容卡片模式（与 GitHub 议题页一致）。
 *
 * 头像走 marker 插槽而不是 avatar 属性——库里多数人没上传头像，只用属性
 * 时组件会退化成一个小圆点图标；走插槽可以复用头像组件的首字母占位，
 * 而且列宽固定 32px，竖线不会因为「有头像/没头像」而错位。
 *
 * 正文用 gh-md 而不是 innerHTML：评论是用户输入，必须先当纯文本交给
 * Markdown 渲染器，绝不能让原始 HTML 直接进 DOM。
 */
function commentTimeline(comments, issue) {
  const timeline = document.createElement('gh-timeline');
  comments.forEach((comment) => {
    const item = document.createElement('gh-timeline-item');
    item.setAttribute('card', '');

    const marker = document.createElement('span');
    marker.setAttribute('slot', 'marker');
    // avatarHTML 认的是 display_name/username，评论对象上叫 author_name：
    // 不转换的话没上传头像的人会显示成「?」
    marker.innerHTML = avatarHTML(
      { display_name: comment.author_name, avatar_url: comment.avatar_url }, 32,
    );
    item.append(marker);

    const header = document.createElement('span');
    header.setAttribute('slot', 'header');
    header.innerHTML = `<strong>${esc(comment.author_name || '访客')}</strong> 评论于 <gh-time datetime="${esc(comment.created_at)}"></gh-time>`;
    item.append(header);

    if (comment.user_id && comment.user_id === issue.author_id) {
      const badge = document.createElement('gh-label');
      badge.setAttribute('slot', 'header-actions');
      badge.setAttribute('color', '#0969da');
      badge.textContent = '作者';
      item.append(badge);
    }

    const body = document.createElement('gh-md');
    body.textContent = comment.body || '';
    item.append(body);

    timeline.append(item);
  });
  return timeline;
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
  const ancestors = Array.isArray(data.ancestors) ? data.ancestors : [];
  const children = Array.isArray(data.children) ? data.children : [];
  const progress = data.progress || { total: 0, closed: 0, percent: 0 };
  const canClose = data.can_close !== false;
  const depth = ancestors.length;
  const assignee = issue.assignee_id
    ? { display_name: issue.assignee_name, avatar_url: issue.assignee_avatar_url }
    : null;

  document.title = `#${issue.number} ${issue.title} · TeamFlow`;

  view.innerHTML = `
    <div class="row" style="gap:8px">
      <a class="small muted row" style="gap:2px" href="/${esc(slug)}/issues"><gh-icon name="chevron-left" size="14"></gh-icon> 返回议题列表</a>
    </div>
    ${ancestors.length ? `<div class="crumbs">
      ${ancestors.map((item) => `<a class="small muted" href="/${esc(slug)}/issues/${esc(item.number)}">#${esc(item.number)} ${esc(item.title)}</a><gh-icon name="chevron-right" size="12"></gh-icon>`).join('')}
      <span class="small">#${esc(issue.number)} ${esc(issue.title)}</span>
    </div>` : ''}

    <gh-page-head title="#${esc(issue.number)} ${esc(issue.title)}">
      <gh-btn size="sm" data-act="refresh"><gh-icon name="sync" size="14"></gh-icon> 刷新</gh-btn>
      ${canEdit && depth + 1 < 3 ? `<gh-btn size="sm" data-act="add-child"><gh-icon name="plus" size="14"></gh-icon> 添加子议题</gh-btn>` : ''}
      ${canEdit ? `<gh-btn size="sm" data-act="toggle-state" ${closed || canClose ? '' : 'disabled'}>${closed ? '重新打开' : '关闭议题'}</gh-btn>
        <gh-btn size="sm" data-act="edit"><gh-icon name="pencil" size="14"></gh-icon> 编辑</gh-btn>
        <gh-btn size="sm" variant="danger" data-act="delete"><gh-icon name="trash" size="14"></gh-icon> 删除</gh-btn>` : ''}
    </gh-page-head>

    ${!closed && !canClose ? `<gh-alert type="warning">还有 ${progress.total - progress.closed} 个子议题未完成，先把子议题关掉再关闭父议题。</gh-alert>` : ''}

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

        ${children.length ? `<gh-box title="子议题 (${progress.closed}/${progress.total})">
          <div class="row" style="justify-content:space-between;margin-bottom:10px">
            <span class="small muted">完成度 ${progress.percent}%</span>
            <span class="small muted">子议题关闭后父议题才可关闭</span>
          </div>
          <div class="sub-progress"><gh-progress value="${progress.percent}" max="100"></gh-progress></div>
          <div class="col" style="gap:2px">
            ${children.map((item) => `<div class="sub-issue" data-number="${esc(item.number)}">
              <gh-icon name="${item.state === 'closed' ? 'issue-closed' : 'issue-opened'}" size="15"></gh-icon>
              <a class="grow" href="/${esc(slug)}/issues/${esc(item.number)}">#${esc(item.number)} ${esc(item.title)}</a>
              ${statusBadge(item.status, true)}
              <span class="small muted">${item.assignee_name ? esc(item.assignee_name) : '未指派'}</span>
            </div>`).join('')}
          </div>
        </gh-box>` : ''}

        <gh-box title="评论 (${comments.length})">
          <div id="comment-host"></div>
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
          ${attrRow('预估工时', issue.estimate_hours ? `${esc(issue.estimate_hours)} 小时` : '')}
          ${attrRow('父议题', issue.parent_id
            ? `<a href="/${esc(slug)}/issues/${esc((ancestors.find((item) => item.id === issue.parent_id) || {}).number || '')}">#${esc((ancestors.find((item) => item.id === issue.parent_id) || {}).number || '')}</a>`
            : '')}
          ${children.length ? attrRow('子议题', `${progress.closed}/${progress.total} 已完成（${progress.percent}%）`) : ''}
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
  const commentHost = view.querySelector('#comment-host');
  if (commentHost) {
    if (comments.length) {
      commentHost.append(commentTimeline(comments, issue));
    } else {
      commentHost.innerHTML = emptyHTML({ icon: 'comment', title: '还没有评论', desc: '在下方写下第一条评论。' });
    }
  }

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
      if (name === 'add-child') {
        openIssueDialog({
          slug,
          defaults: { parent_id: issue.id, milestone_id: issue.milestone_id || null },
          onSaved: () => { invalidateTeam(slug); render(view, route); },
        });
        return;
      }
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
