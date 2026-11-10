/* TeamFlow · 设置 */

import { api } from '../api.js';
import { S, invalidateTeam, loadTeam, bootstrap, members as teamMembers } from '../store.js';
import { navigate } from '../router.js';
import {
  esc, pageHead, avatarHTML, toast, confirmAction, openDialog, fieldHTML, askText, SPINNER,
  onView,
} from '../ui.js';

const ROLE_LABELS = { owner: '拥有者', admin: '管理员', member: '成员' };

function isAdminRole(role) {
  return role === 'owner' || role === 'admin';
}

function publicSwitch(name, label, checked, hint) {
  return `<gh-field label="" hint="${esc(hint || '')}">
    <gh-switch name="${esc(name)}" status status-on="公开" status-off="仅成员"${checked ? ' checked' : ''}></gh-switch>
    <span class="small">${esc(label)}</span>
  </gh-field>`;
}

function skillChips(member) {
  const skills = Array.isArray(member.skills) ? member.skills : [];
  if (!skills.length) return '';
  return `<div class="row" style="gap:4px;margin-top:4px">${skills
    .map((item) => `<gh-label color="${item.level >= 3 ? '#0969da' : (item.level === 2 ? '#8250df' : '#6b7280')}">${esc(item.name)}</gh-label>`)
    .join('')}</div>`;
}

function memberRow(member, canEdit, isOwner) {
  return `<div class="row" style="padding:10px 0;border-bottom:1px solid var(--gh-border-muted)">
    ${avatarHTML(member, 32)}
    <div class="grow">
      <div><b>${esc(member.display_name || member.username)}</b> <span class="small muted mono">@${esc(member.username)}</span></div>
      <div class="small muted">${esc(member.title || '—')}${member.can_login ? '' : ' · 仅记录身份'}</div>
      ${skillChips(member)}
    </div>
    <gh-badge state="${member.role === 'owner' ? 'accent' : (member.role === 'admin' ? 'warn' : 'open')}">${esc(ROLE_LABELS[member.role] || member.role || '成员')}</gh-badge>
    ${canEdit && member.role !== 'owner' ? `
      <gh-btn size="sm" variant="invisible" data-act="edit-member" data-username="${esc(member.username)}"><gh-icon name="pencil" size="12"></gh-icon></gh-btn>
      <gh-btn size="sm" variant="invisible" data-act="avatar" data-username="${esc(member.username)}"><gh-icon name="image" size="12"></gh-icon></gh-btn>
      ${isOwner ? `<gh-btn size="sm" variant="invisible" data-act="remove-member" data-username="${esc(member.username)}"><gh-icon name="trash" size="12"></gh-icon></gh-btn>` : ''}
    ` : ''}
  </div>`;
}

function labelRow(label, canEdit) {
  return `<div class="label-row">
    <span class="swatch" style="background:${esc(label.color || '#6b7280')}"></span>
    <span class="grow">${esc(label.name)}</span>
    <span class="small muted">${label.uses ?? 0} 次使用</span>
    ${canEdit ? `<gh-btn size="sm" variant="invisible" data-act="edit-label" data-name="${esc(label.name)}"><gh-icon name="pencil" size="12"></gh-icon></gh-btn>
      <gh-btn size="sm" variant="invisible" data-act="delete-label" data-name="${esc(label.name)}"><gh-icon name="trash" size="12"></gh-icon></gh-btn>` : ''}
  </div>`;
}

export function openLabelDialog(label = null, onSaved = null) {
  const slug = S.detail.slug;
  return openDialog({
    title: label ? `编辑标签「${label.name}」` : '新建标签',
    okText: label ? '保存' : '创建',
    body: `${fieldHTML({ label: '名称', name: 'name', placeholder: '例如：前端', maxlength: 32 })}
      ${fieldHTML({ label: '颜色', name: 'color', type: 'color' })}`,
    onMount: (dialog) => {
      const name = dialog.querySelector('[name="name"]');
      if (name) name.value = label ? label.name : '';
      const color = dialog.querySelector('[name="color"]');
      if (color) color.value = label ? (label.color || '#6b7280') : '#0969da';
    },
    onSubmit: async (values) => {
      const name = String(values.name || '').trim();
      if (!name) { toast('请填写标签名称', 'err'); return false; }
      const color = String(values.color || '#6b7280');
      if (label) await api.patch(`/api/teams/${encodeURIComponent(slug)}/labels/${encodeURIComponent(label.name)}`, { name, color });
      else await api.post(`/api/teams/${encodeURIComponent(slug)}/labels`, { name, color });
      toast(label ? '标签已保存' : '标签已创建');
      if (onSaved) await onSaved();
      return true;
    },
  });
}

export function openMemberDialog(member = null, onSaved = null) {
  const slug = S.detail.slug;
  const current = member || {};
  const skills = Array.isArray(current.skills) ? current.skills : [];
  const skillText = skills.map((item) => `${item.name}${item.level >= 3 ? '!' : (item.level === 2 ? '+' : '')}`).join(', ');
  const body = `
    <div class="form-grid">
      ${fieldHTML({ label: '显示名称', name: 'display_name', placeholder: '张三' })}
      ${fieldHTML({ label: '职位', name: 'title', placeholder: '前端工程师' })}
      ${member ? '' : fieldHTML({ label: '登录名（可选）', name: 'username', hint: '填写后会创建可登录的账号' })}
      <gh-field label="角色"><gh-select name="role">
        <option value="member"${current.role === 'member' ? ' selected' : ''}>成员</option>
        <option value="admin"${current.role === 'admin' ? ' selected' : ''}>管理员</option>
      </gh-select></gh-field>
    </div>
    ${fieldHTML({ label: '技能', name: 'skills', value: skillText, hint: '逗号分隔；后缀 + 表示熟练、! 表示精通，例如：前端!, 登录+' })}
    <div class="small muted">管理员可以修改团队设置与全部议题；成员可以编辑议题与日程。技能用于引擎的负责人建议。</div>`;

  return openDialog({
    title: member ? `编辑成员 ${member.display_name || member.username}` : '添加成员',
    okText: member ? '保存' : '添加',
    body,
    onMount: (dialog) => {
      const name = dialog.querySelector('[name="display_name"]');
      if (name) name.value = current.display_name || '';
      const title = dialog.querySelector('[name="title"]');
      if (title) title.value = current.title || '';
    },
    onSubmit: async (values) => {
      const displayName = String(values.display_name || '').trim();
      if (!displayName) { toast('请填写显示名称', 'err'); return false; }
      const payload = {
        display_name: displayName,
        title: String(values.title || ''),
        role: values.role || 'member',
      };
      const skillsValue = parseSkillText(values.skills);
      if (member) {
        payload.skills = skillsValue;
        await api.patch(`/api/teams/${encodeURIComponent(slug)}/members/${encodeURIComponent(member.username)}`, payload);
        toast('成员信息已保存');
      } else {
        const username = String(values.username || '').trim();
        if (username) payload.username = username;
        await api.post(`/api/teams/${encodeURIComponent(slug)}/members`, payload);
        toast('成员已添加');
      }
      if (onSaved) await onSaved();
      return true;
    },
  });
}

/* "前端!, 登录+" → [{name:'前端', level:3}, {name:'登录', level:2}] */
function parseSkillText(text) {
  const raw = String(text || '');
  return raw.split(/[,，;；\n]/).map((part) => part.trim()).filter(Boolean).slice(0, 20).map((part) => {
    const match = /^(.*?)([!+＋！])?$/.exec(part);
    const name = String((match && match[1]) || part).trim().slice(0, 24);
    const mark = match && match[2];
    const level = mark === '!' || mark === '！' ? 3 : (mark === '+' || mark === '＋' ? 2 : 1);
    return { name, level, source: 'self' };
  }).filter((item) => item.name);
}

function pickAvatarFile() {
  return new Promise((resolve) => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = 'image/png,image/jpeg,image/gif,image/webp';
    input.addEventListener('change', () => resolve(input.files && input.files[0] ? input.files[0] : null));
    input.click();
  });
}

export async function render(view, route) {
  const slug = S.detail.slug;
  view.innerHTML = SPINNER;

  let detail;
  let labels = [];
  let editStatus = { enabled: false, locked: false };
  try {
    detail = await loadTeam(slug, { force: true });
  } catch (error) {
    view.innerHTML = `<div class="error-box"><gh-icon name="alert" size="24"></gh-icon>
      <div class="error-box__title">设置加载失败</div>
      <div class="error-box__msg">${esc(error.message || '')}</div></div>`;
    return;
  }
  try {
    const labelData = await api.get(`/api/teams/${encodeURIComponent(slug)}/labels`);
    labels = Array.isArray(labelData.labels) ? labelData.labels : [];
  } catch { labels = []; }
  try { editStatus = await api.get('/api/edit/status'); } catch { /* 忽略 */ }

  const team = detail || S.detail || {};
  const canEdit = !!team.can_edit;
  const role = team.role || '';
  const admin = isAdminRole(role);
  const memberList = Array.isArray(team.members) ? team.members : [];

  view.innerHTML = `${pageHead('设置', '团队资料、成员、标签与编辑权限', `
      <gh-btn size="sm" data-act="refresh"><gh-icon name="sync" size="14"></gh-icon> 刷新</gh-btn>
    `)}
    <div class="settings-grid">
      <gh-box title="团队资料" class="span-2">
        <div class="form-grid">
          ${fieldHTML({ label: '团队名称', name: 'name', value: team.name || '', maxlength: 80 })}
          ${fieldHTML({ label: '团队标识', name: 'slug', value: team.slug || '', hint: '用于访问地址 /{slug}/…' })}
          ${fieldHTML({ label: '简介', name: 'description', type: 'textarea', rows: 3, value: team.description || '' })}
        </div>
        <div class="form-grid">
          ${publicSwitch('issues_public', '议题对外公开', team.issues_public, '关闭后仅团队成员可见')}
          ${publicSwitch('milestones_public', '里程碑对外公开', team.milestones_public)}
          ${publicSwitch('roadmap_public', '路线图对外公开', team.roadmap_public)}
          ${publicSwitch('schedules_public', '日程对外公开', team.schedules_public)}
        </div>
        <div class="form-actions">
          <gh-btn variant="primary" size="sm" data-act="save-team">保存团队资料</gh-btn>
        </div>
      </gh-box>

      <gh-box>
        <div slot="bar" class="box-bar">
          <b>成员 (${memberList.length})</b>
          <span class="grow"></span>
          ${canEdit ? '<gh-btn size="sm" data-act="add-member"><gh-icon name="plus" size="12"></gh-icon> 添加成员</gh-btn>' : ''}
        </div>
        ${memberList.length ? memberList.map((member) => memberRow(member, canEdit, role === 'owner')).join('') : '<div class="small muted">暂不可见成员列表（需要编辑权限）。</div>'}
      </gh-box>

      <gh-box>
        <div slot="bar" class="box-bar">
          <b>标签</b>
          <span class="grow"></span>
          ${canEdit ? '<gh-btn size="sm" data-act="add-label"><gh-icon name="plus" size="12"></gh-icon> 新建标签</gh-btn>' : ''}
        </div>
        ${labels.length ? labels.map((label) => labelRow(label, canEdit)).join('') : '<div class="small muted">还没有标签。</div>'}
      </gh-box>

      <gh-box title="编辑权限">
        <div class="small muted" style="margin-bottom:8px">
          ${S.requireLogin
            ? '当前已开启登录：写操作需要登录后由成员完成。'
            : `当前未开启登录，使用「操作身份」模拟成员；${editStatus.enabled ? '设置编辑口令后，写入前需要解锁。' : '尚未设置编辑口令，任何人都可以编辑。'}`}
        </div>
        <div class="row">
          <gh-badge state="${editStatus.enabled ? 'accent' : 'open'}">${editStatus.enabled ? '已设置编辑口令' : '无编辑口令'}</gh-badge>
          <gh-badge state="${editStatus.locked ? 'warn' : 'closed'}">${editStatus.locked ? '当前未解锁' : '当前可编辑'}</gh-badge>
        </div>
        <div class="form-actions">
          ${editStatus.locked ? '<gh-btn size="sm" data-act="unlock">输入口令解锁</gh-btn>' : ''}
          ${admin ? `<gh-btn size="sm" data-act="set-password">${editStatus.enabled ? '修改编辑口令' : '设置编辑口令'}</gh-btn>` : ''}
        </div>
      </gh-box>

      ${admin ? `<gh-box title="登录与账号">
        <div class="row" style="justify-content:space-between;align-items:center">
          <div>
            <div>要求登录后编辑</div>
            <div class="small muted">开启后，所有人都需要登录才能创建或修改内容。</div>
          </div>
          <gh-switch name="require_login" status status-on="已开启" status-off="已关闭"${S.requireLogin ? ' checked' : ''}></gh-switch>
        </div>
      </gh-box>` : ''}

      ${admin ? `<gh-box title="AI 助手" class="span-2">
        <div id="ai-config" class="col" style="gap:8px"><div class="small muted">加载中…</div></div>
      </gh-box>` : ''}

      ${role === 'owner' ? `<gh-box title="危险操作" class="span-2">
        <div class="row" style="justify-content:space-between;align-items:center">
          <div>
            <div>重置团队数据</div>
            <div class="small muted">清空该团队的全部议题、里程碑、日程与标签，保留团队本身。</div>
          </div>
          <gh-btn size="sm" variant="danger" data-act="reset-team">重置团队</gh-btn>
        </div>
      </gh-box>` : ''}
    </div>`;

  /* AI 配置 */
  if (admin) {
    const host = view.querySelector('#ai-config');
    api.get('/api/ai/config').then((config) => {
      if (!host) return;
      host.innerHTML = `
        ${fieldHTML({ label: '接口地址', name: 'ai_base_url', value: config.base_url || '', placeholder: 'https://api.example.com/v1' })}
        ${fieldHTML({ label: '模型', name: 'ai_model', value: config.model || '', placeholder: 'deepseek-chat' })}
        ${fieldHTML({ label: 'API Key', name: 'ai_api_key', type: 'password', placeholder: config.api_key_masked || '未配置' })}
        <div class="row">
          <gh-badge state="${config.configured ? 'closed' : 'open'}">${config.configured ? '已配置' : '未配置'}</gh-badge>
          <span class="grow"></span>
          <gh-btn size="sm" data-act="save-ai">保存</gh-btn>
        </div>`;
    }).catch((error) => {
      if (host) host.innerHTML = `<div class="small muted">${esc(error.message || '无法读取 AI 配置')}</div>`;
    });
  }

  const refresh = () => { invalidateTeam(slug); render(view, route); };

  onView(view, 'click', async (event) => {
    const act = event.target.closest('[data-act]');
    if (!act) return;
    const name = act.getAttribute('data-act');
    try {
      if (name === 'refresh') { refresh(); return; }
      if (name === 'save-team') {
        const value = (key) => {
          const node = view.querySelector(`[name="${key}"]`);
          return node ? node.value : '';
        };
        const switchOn = (key) => {
          const node = view.querySelector(`gh-switch[name="${key}"]`);
          return node ? (node.querySelector('input') ? node.querySelector('input').checked : node.hasAttribute('checked')) : false;
        };
        await api.patch(`/api/teams/${encodeURIComponent(slug)}`, {
          name: String(value('name') || '').trim(),
          slug: String(value('slug') || '').trim(),
          description: String(value('description') || ''),
          issues_public: switchOn('issues_public'),
          milestones_public: switchOn('milestones_public'),
          roadmap_public: switchOn('roadmap_public'),
          schedules_public: switchOn('schedules_public'),
        });
        toast('团队资料已保存');
        await bootstrap();
        refresh();
        return;
      }
      if (name === 'add-member') { openMemberDialog(null, refresh); return; }
      if (name === 'edit-member') {
        const username = act.getAttribute('data-username');
        const member = teamMembers().find((m) => m.username === username);
        if (member) openMemberDialog(member, refresh);
        return;
      }
      if (name === 'avatar') {
        const username = act.getAttribute('data-username');
        const file = await pickAvatarFile();
        if (!file) return;
        const form = new FormData();
        form.append('file', file);
        await api.upload(`/api/teams/${encodeURIComponent(slug)}/members/${encodeURIComponent(username)}/avatar`, form);
        toast('头像已更新');
        refresh();
        return;
      }
      if (name === 'remove-member') {
        const username = act.getAttribute('data-username');
        const ok = await confirmAction({ title: '移除成员', message: `确定把 @${username} 移出团队吗？`, okText: '移除', danger: true });
        if (!ok) return;
        await api.del(`/api/teams/${encodeURIComponent(slug)}/members/${encodeURIComponent(username)}`);
        toast('成员已移除');
        refresh();
        return;
      }
      if (name === 'add-label') { openLabelDialog(null, refresh); return; }
      if (name === 'edit-label') {
        const label = labels.find((item) => item.name === act.getAttribute('data-name'));
        if (label) openLabelDialog(label, refresh);
        return;
      }
      if (name === 'delete-label') {
        const labelName = act.getAttribute('data-name');
        const ok = await confirmAction({ title: '删除标签', message: `确定删除标签「${labelName}」吗？`, okText: '删除', danger: true });
        if (!ok) return;
        await api.del(`/api/teams/${encodeURIComponent(slug)}/labels/${encodeURIComponent(labelName)}`);
        toast('标签已删除');
        refresh();
        return;
      }
      if (name === 'set-password') {
        const next = await askText({
          title: editStatus.enabled ? '修改编辑口令' : '设置编辑口令',
          label: '留空表示删除口令',
          okText: '保存',
          placeholder: '新的编辑口令',
        });
        if (next === null || next === undefined) return;
        const result = await api.post('/api/edit/password', { next: String(next) });
        toast(result.enabled ? '编辑口令已更新' : '编辑口令已删除');
        refresh();
        return;
      }
      if (name === 'unlock') {
        const password = await askText({ title: '解锁编辑', label: '请输入编辑口令', okText: '解锁' });
        if (!password) return;
        await api.post('/api/edit/unlock', { password });
        S.editLocked = false;
        toast('已解锁');
        refresh();
        return;
      }
      if (name === 'save-ai') {
        const value = (key) => {
          const node = view.querySelector(`#ai-config [name="${key}"]`);
          return node ? String(node.value || '').trim() : '';
        };
        await api.post('/api/ai/config', {
          base_url: value('ai_base_url'),
          model: value('ai_model'),
          api_key: value('ai_api_key'),
        });
        toast('AI 配置已保存');
        refresh();
        return;
      }
      if (name === 'reset-team') {
        const ok1 = await confirmAction({
          title: '重置团队数据',
          message: '这会清空议题、里程碑、标签与日程。请输入确认以继续。',
          okText: '继续',
          danger: true,
        });
        if (!ok1) return;
        const word = await askText({ title: '确认重置', label: '请输入 DELETE 以确认', okText: '重置', placeholder: 'DELETE' });
        if (String(word || '').trim().toUpperCase() !== 'DELETE') { toast('已取消', 'err'); return; }
        await api.post(`/api/teams/${encodeURIComponent(slug)}/reset`, { confirm: 'DELETE' });
        toast('团队数据已重置');
        invalidateTeam();
        await bootstrap();
        navigate(`/${slug}/overview`);
        return;
      }
    } catch (error) {
      toast(error.message || '操作失败', 'err');
    }
  });

  const loginSwitch = view.querySelector('gh-switch[name="require_login"]');
  if (loginSwitch) {
    loginSwitch.addEventListener('change', async (event) => {
      const input = event.target.closest('input') || loginSwitch.querySelector('input');
      const enabled = input ? input.checked : loginSwitch.hasAttribute('checked');
      try {
        const result = await api.post('/api/settings/require-login', { enabled });
        S.requireLogin = !!result.require_login;
        toast(result.require_login ? '已开启登录编辑' : '已关闭登录编辑');
        await bootstrap();
        refresh();
      } catch (error) {
        toast(error.message || '设置失败', 'err');
        if (input) input.checked = !enabled;
      }
    });
  }
}
