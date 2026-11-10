/* TeamFlow · 应用外壳（侧栏 + 顶栏 + 视图容器） */

import { api } from './api.js';
import { navigate } from './router.js';
import {
  S, actorId, setActorId, members, toggleTheme, currentTheme,
  bootstrap, invalidateTeam,
} from './store.js';
import { esc, avatarHTML, openDialog, toast, confirmAction } from './ui.js';

export const NAV = [
  { view: 'overview', icon: 'home', label: '概览' },
  { view: 'issues', icon: 'issue-opened', label: '议题' },
  { view: 'board', icon: 'project', label: '看板' },
  { view: 'week', icon: 'calendar', label: '周计划' },
  { view: 'milestones', icon: 'milestone', label: '里程碑' },
  { view: 'roadmap', icon: 'project-roadmap', label: '路线图' },
  { view: 'schedule', icon: 'clock', label: '日程' },
  { view: 'engine', icon: 'rocket', label: '引擎' },
  { view: 'settings', icon: 'gear', label: '设置' },
];

export const VIEW_TITLES = {
  overview: '概览',
  issues: '议题',
  issue: '议题详情',
  board: '看板',
  week: '周计划',
  milestones: '里程碑',
  roadmap: '路线图',
  schedule: '日程',
  engine: '推理引擎',
  settings: '设置',
};

let bound = false;

function navHTML() {
  const counts = (S.detail && S.detail.counts) || {};
  return NAV.map((item) => {
    const count = item.view === 'issues' && counts.open ? ` count="${esc(counts.open)}"` : '';
    return `<gh-nav-item key="${item.view}" data-view="${item.view}" icon="${item.icon}"${count}>${item.label}</gh-nav-item>`;
  }).join('');
}

function identityHTML() {
  if (S.requireLogin) return '';
  const list = members();
  if (!list.length) return '';
  const current = actorId();
  const options = [
    `<option value=""${current ? '' : ' selected'}>访客（不显示为团队成员）</option>`,
    ...list.map((m) => `<option value="${esc(m.id)}"${String(m.id) === current ? ' selected' : ''}>${esc(m.display_name || m.username)}</option>`),
  ].join('');
  return `<div class="side-identity">
    <span class="side-identity__label">操作身份</span>
    <gh-select id="actor-select" size="sm">${options}</gh-select>
  </div>`;
}

function userFootHTML() {
  const user = S.user;
  const person = user || { display_name: '访客', username: 'guest' };
  const meta = user
    ? `@${esc(user.username)}${user.title ? ` · ${esc(user.title)}` : ''}`
    : (S.requireLogin ? '未登录' : '匿名访客');
  return `<div class="side-user">
    ${avatarHTML(person, 26)}
    <div class="side-user__text">
      <span class="side-user__name">${esc(person.display_name || person.username || '访客')}</span>
      <span class="side-user__meta">${meta}</span>
    </div>
  </div>`;
}

export function shellHTML() {
  const team = S.detail || S.team || {};
  const theme = currentTheme();
  return `<gh-app id="shell">
    <gh-side id="side">
      <gh-account id="team-account" name="${esc(team.name || 'TeamFlow')}" subtitle="团队工作区" action-icon="chevron-down"></gh-account>
      <gh-nav-title>工作区</gh-nav-title>
      ${navHTML()}
      <div slot="foot" class="side-foot">
        ${userFootHTML()}
        ${identityHTML()}
        <div class="side-tools">
          <gh-btn id="theme-btn" size="sm" variant="invisible" data-act="theme" title="切换主题"><gh-icon name="${theme === 'dark' ? 'sun' : 'moon'}"></gh-icon></gh-btn>
          <gh-btn size="sm" variant="invisible" data-act="switch-team" title="切换团队"><gh-icon name="arrow-switch"></gh-icon></gh-btn>
          <span class="grow"></span>
          <gh-btn size="sm" variant="invisible" data-act="auth">${S.user ? '退出' : '登录'}</gh-btn>
        </div>
      </div>
    </gh-side>
    <gh-topbar id="topbar">
      <gh-crumbs id="crumbs"></gh-crumbs>
      <span class="grow"></span>
      <div class="topbar-extra" id="topbar-extra">
        <gh-input id="topbar-search" class="topbar-search" size="sm" icon="search" placeholder="搜索议题…" aria-label="搜索议题"></gh-input>
      </div>
      ${S.user
        ? `<div class="topbar-user">${avatarHTML(S.user, 24)}</div>`
        : (S.requireLogin ? `<a href="/login">登录</a>` : '<span class="muted small">访客</span>')}
    </gh-topbar>
    <div class="view" id="view"></div>
  </gh-app>`;
}

function structuralKey() {
  return [S.detail && S.detail.id, S.user && S.user.id, S.role, S.canEdit ? 1 : 0, S.requireLogin ? 1 : 0, actorId()].join('|');
}

export function renderShell(route) {
  const app = document.getElementById('app');
  if (!app) return null;
  if (app.dataset.shellKey !== structuralKey() || !document.getElementById('view')) {
    app.innerHTML = shellHTML();
    app.dataset.shellKey = structuralKey();
    bindShell();
  }
  setActiveNav(route && route.view);
  updateCrumbs(route);
  return document.getElementById('view');
}

export function updateCrumbs(route) {
  const crumbs = document.getElementById('crumbs');
  if (!crumbs) return;
  const team = S.detail || S.team || {};
  const title = route && route.view ? (VIEW_TITLES[route.view] || route.view) : '概览';
  const slug = team.slug || '';
  let parts = '';
  if (route && route.view === 'issue') {
    parts = `<a href="/${esc(slug)}/overview">${esc(team.name || '团队')}</a><a href="/${esc(slug)}/issues">议题</a><span>#${esc(route.arg || '')}</span>`;
  } else {
    parts = `<a href="/${esc(slug)}/overview">${esc(team.name || '团队')}</a><span>${esc(title)}</span>`;
  }
  crumbs.innerHTML = parts;
}

export function setActiveNav(view) {
  const side = document.getElementById('side');
  if (!side) return;
  side.querySelectorAll('gh-nav-item').forEach((item) => {
    const isActive = item.dataset.view === view;
    item.toggleAttribute('active', isActive);
    item.classList.toggle('is-active', isActive);
  });
}

export function refreshShell(route) {
  const side = document.getElementById('side');
  if (!side) return;
  const nav = side.querySelector('gh-nav-title + gh-nav-item');
  if (!nav) return;
  const team = S.detail || {};
  const account = document.getElementById('team-account');
  if (account && team.name) {
    account.setAttribute('name', team.name);
  }
  const counts = (team.counts) || {};
  side.querySelectorAll('gh-nav-item').forEach((item) => {
    if (item.dataset.view === 'issues') {
      const value = counts.open ? String(counts.open) : '';
      if (value) item.setAttribute('count', value);
      else item.removeAttribute('count');
    }
  });
  if (route) setActiveNav(route.view);
}

function bindShell() {
  const side = document.getElementById('side');
  if (!side) return;

  side.addEventListener('gh:nav', (event) => {
    const item = event.target.closest('gh-nav-item');
    const view = item && item.dataset.view;
    if (!view) return;
    const team = S.detail || S.team;
    if (!team) return;
    navigate(`/${team.slug}/${view}`);
  });

  side.addEventListener('click', async (event) => {
    const account = event.target.closest('#team-account');
    if (account) { openTeamSwitcher(); return; }
    const action = event.target.closest('[data-act]');
    if (!action) return;
    const act = action.getAttribute('data-act');
    if (act === 'theme') {
      const next = toggleTheme();
      const icon = action.querySelector('gh-icon');
      if (icon) icon.setAttribute('name', next === 'dark' ? 'sun' : 'moon');
    } else if (act === 'switch-team') {
      openTeamSwitcher();
    } else if (act === 'auth') {
      handleAuthAction();
    }
  });

  const actorSelect = document.getElementById('actor-select');
  if (actorSelect) {
    actorSelect.addEventListener('change', (event) => {
      const value = event.target && event.target.value !== undefined ? event.target.value : actorSelect.value;
      setActorId(value || '');
      invalidateTeam();
      toast(value ? '已切换操作身份' : '已切换为访客');
      const route = window.__tfRoute;
      navigate(location.pathname + location.search, { replace: true });
      void route;
    });
  }

  const search = document.getElementById('topbar-search');
  if (search) {
    const input = search.querySelector('input');
    if (input) {
      input.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter') return;
        event.preventDefault();
        const q = input.value.trim();
        const team = S.detail || S.team;
        if (!team) return;
        navigate(`/${team.slug}/issues${q ? `?q=${encodeURIComponent(q)}` : ''}`);
      });
    }
  }
}

export async function handleAuthAction() {
  if (!S.user) { navigate('/login'); return; }
  const ok = await confirmAction({ title: '退出登录', message: '确定要退出当前账号吗？', okText: '退出' });
  if (!ok) return;
  try {
    await api.post('/api/auth/logout');
    await bootstrap();
    invalidateTeam();
    location.href = S.requireLogin ? '/login' : '/';
  } catch (error) {
    toast(error.message || '退出失败', 'err');
  }
}

export async function openTeamSwitcher() {
  const rows = S.teams.map((team) => {
    const current = S.detail && team.id === S.detail.id;
    return `<gh-list-item icon="organization" title="${esc(team.name)}" meta="/${esc(team.slug)}"${current ? ' selected' : ''} data-slug="${esc(team.slug)}"></gh-list-item>`;
  }).join('');
  const body = `<div class="dlg"><div class="dlg-list">${rows || '<p class="muted">还没有团队</p>'}</div></div>
    <div slot="footer" class="row" style="width:100%">
      ${S.user ? '<gh-btn size="sm" variant="invisible" data-act="create"><gh-icon name="plus"></gh-icon> 新建团队</gh-btn>' : ''}
      <span class="grow"></span>
      <gh-btn size="sm" variant="invisible" data-act="manage">全部团队</gh-btn>
      <gh-btn size="sm" data-gh-close>关闭</gh-btn>
    </div>`;
  const dialog = openDialog({
    title: '切换团队',
    body,
    okText: null,
    cancelText: null,
    onMount: (dlg) => {
      dlg.addEventListener('click', (event) => {
        const item = event.target.closest('[data-slug]');
        if (item) {
          const slug = item.getAttribute('data-slug');
          dlg.close();
          navigate(`/${slug}/overview`);
          return;
        }
        const act = event.target.closest('[data-act]');
        if (!act) return;
        if (act.getAttribute('data-act') === 'create') { dlg.close(); openCreateTeam(); }
        if (act.getAttribute('data-act') === 'manage') { dlg.close(); navigate('/teams'); }
      });
    },
  });
  return dialog;
}

export function openCreateTeam(onCreated) {
  const dialog = openDialog({
    title: '新建团队',
    okText: '创建',
    body: `
      <gh-field label="团队名称"><gh-input name="name" placeholder="例如：产品研发团队"></gh-input></gh-field>
      <gh-field label="标识（URL）" hint="留空自动生成，仅小写字母、数字与短横线"><gh-input name="slug" placeholder="product-team"></gh-input></gh-field>
      <gh-field label="简介"><gh-textarea name="description" rows="3" placeholder="一句话说明团队目标"></gh-textarea></gh-field>`,
    onSubmit: async (values) => {
      const name = String(values.name || '').trim();
      if (!name) { toast('请填写团队名称', 'err'); return false; }
      const payload = { name };
      const slug = String(values.slug || '').trim();
      if (slug) payload.slug = slug;
      const description = String(values.description || '').trim();
      if (description) payload.description = description;
      const data = await api.post('/api/teams', payload);
      await bootstrap();
      invalidateTeam();
      toast('团队已创建');
      if (onCreated) onCreated(data.team);
      else navigate(`/${data.team.slug}/overview`);
      return true;
    },
  });
  return dialog;
}
