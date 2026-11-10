/* TeamFlow · 登录 / 注册 / 团队列表 */

import { api } from '../api.js';
import { S, bootstrap, invalidateTeam, actorId } from '../store.js';
import { navigate } from '../router.js';
import { esc, avatarHTML, openDialog, emptyHTML, toast, timeAgo,
  onView,
} from '../ui.js';

export function renderLogin(app) {
  document.title = '登录 · TeamFlow';
  const demo = (S.demoAccounts || []).slice();
  const demoRows = demo.map((account) => `<div class="auth-demo__row" data-username="${esc(account.username)}">
      <gh-icon name="person" size="14"></gh-icon>
      <b>${esc(account.display_name || account.username)}</b>
      <span class="mono">@${esc(account.username)}</span>
      <span class="grow"></span>
      <span class="mono">${esc(S.demoPassword || '')}</span>
    </div>`).join('');

  app.innerHTML = `<div class="center-wrap">
    <div class="auth-card">
      <div class="auth-brand">
        <span class="auth-brand__mark"><gh-icon name="git-merge" size="16"></gh-icon></span>
        <span>TeamFlow</span>
      </div>
      <p class="muted small">登录后即可在多个团队之间切换，并以真实成员身份留下动态记录。</p>
      <gh-field label="用户名"><gh-input name="username" placeholder="linwei" autocomplete="username"></gh-input></gh-field>
      <gh-field label="密码"><gh-input name="password" type="password" reveal placeholder="请输入密码" autocomplete="current-password"></gh-input></gh-field>
      <div class="form-actions">
        <gh-btn variant="invisible" data-act="register">注册新账号</gh-btn>
        <gh-btn variant="primary" data-act="login">登录</gh-btn>
      </div>
      ${demo.length ? `<div class="auth-demo">
        <div class="small">演示账号（点击填充）</div>
        ${demoRows}
      </div>` : ''}
      <a class="small muted" href="/${S.teams && S.teams[0] ? esc(S.teams[0].slug) + '/overview' : ''}">以访客身份继续浏览 →</a>
    </div>
  </div>`;

  const card = app.querySelector('.auth-card');
  const usernameInput = card.querySelector('[name="username"]');
  const passwordInput = card.querySelector('[name="password"]');

  const submit = async () => {
    const username = String(usernameInput ? usernameInput.value : '').trim();
    const password = String(passwordInput ? passwordInput.value : '');
    if (!username || !password) { toast('请输入用户名和密码', 'err'); return; }
    try {
      await api.post('/api/auth/login', { username, password });
      await bootstrap();
      invalidateTeam();
      toast('登录成功');
      navigate('/', { replace: true });
    } catch (error) {
      toast(error.message || '登录失败', 'err');
    }
  };

  card.addEventListener('click', (event) => {
    const act = event.target.closest('[data-act]');
    if (act) {
      const name = act.getAttribute('data-act');
      if (name === 'login') submit();
      if (name === 'register') openRegister();
      return;
    }
    const row = event.target.closest('[data-username]');
    if (row && usernameInput) {
      usernameInput.value = row.getAttribute('data-username');
      if (passwordInput) passwordInput.value = S.demoPassword || '';
    }
  });

  card.querySelectorAll('gh-input').forEach((field) => {
    const input = field.querySelector('input');
    if (!input) return;
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); submit(); }
    });
  });

  onView(app, 'click', (event) => {
    if (event.target.closest('a[href]')) return;
  });
}

export function openRegister() {
  openDialog({
    title: '注册新账号',
    okText: '注册',
    body: `
      <gh-field label="用户名" hint="3-24 位，小写字母、数字与 . _ -"><gh-input name="username" placeholder="zhangsan"></gh-input></gh-field>
      <gh-field label="显示名称"><gh-input name="display_name" placeholder="张三"></gh-input></gh-field>
      <gh-field label="密码" hint="至少 6 位"><gh-input name="password" type="password" reveal></gh-input></gh-field>`,
    onSubmit: async (values) => {
      const username = String(values.username || '').trim();
      const password = String(values.password || '');
      const displayName = String(values.display_name || '').trim();
      if (!username || !password) { toast('请填写用户名与密码', 'err'); return false; }
      await api.post('/api/auth/register', {
        username,
        password,
        display_name: displayName || username,
      });
      await bootstrap();
      invalidateTeam();
      toast('注册成功');
      navigate('/', { replace: true });
      return true;
    },
  });
}

export async function renderTeams(app) {
  document.title = '全部团队 · TeamFlow';
  let teams = S.teams || [];
  try {
    const data = await api.get('/api/teams');
    teams = data.teams || [];
    S.teams = teams;
  } catch (error) {
    app.innerHTML = `<div class="center-wrap"><div class="auth-card">
      <div class="auth-brand"><span class="auth-brand__mark"><gh-icon name="alert" size="16"></gh-icon></span>TeamFlow</div>
      <p class="error-box__msg">${esc(error.message || '无法加载团队列表')}</p>
      <gh-btn data-act="home">返回首页</gh-btn>
    </div></div>`;
    onView(app, 'click', (event) => {
      if (event.target.closest('[data-act="home"]')) location.href = '/';
    });
    return;
  }

  const cards = teams.map((team) => {
    const flags = [
      team.issues_public ? '议题公开' : '',
      team.milestones_public ? '里程碑公开' : '',
      team.roadmap_public ? '路线图公开' : '',
      team.schedules_public ? '日程公开' : '',
    ].filter(Boolean);
    return `<div class="team-card" data-slug="${esc(team.slug)}">
      <div class="row">
        <gh-avatar size="24">${esc((team.name || '?').slice(0, 2))}</gh-avatar>
        <span class="team-card__name">${esc(team.name)}</span>
        <span class="grow"></span>
        ${team.role ? `<gh-badge state="accent">${esc(team.role)}</gh-badge>` : ''}
      </div>
      <div class="team-card__desc">${esc(team.description || '暂无简介')}</div>
      <div class="row small muted">
        <span class="mono">/${esc(team.slug)}</span>
        ${flags.length ? `<span>·</span><span>${esc(flags.join(' · '))}</span>` : ''}
      </div>
    </div>`;
  }).join('');

  app.innerHTML = `<div class="center-wrap center-wrap--top">
    <div class="teams-page">
      <div class="teams-head">
        <div class="col" style="gap:2px">
          <div class="row small"><a class="muted" href="/">← 返回</a></div>
          <h2 class="teams-head__title">团队</h2>
          <p class="small muted">${teams.length ? `共 ${teams.length} 个团队，点击卡片进入工作区。` : '还没有可访问的团队。'}</p>
        </div>
        <div class="row">
          ${S.user ? '<gh-btn variant="primary" size="sm" data-act="create"><gh-icon name="plus"></gh-icon> 新建团队</gh-btn>' : '<a class="small" href="/login">登录后创建团队</a>'}
        </div>
      </div>
      ${teams.length ? `<div class="team-grid">${cards}</div>` : emptyHTML({ icon: 'organization', title: '还没有团队', desc: '登录后可以创建第一个团队。' })}
      <p class="small muted">当前身份：${S.user ? esc(S.user.display_name || S.user.username) : '访客（未登录）'}</p>
    </div>
  </div>`;

  onView(app, 'click', (event) => {
    const card = event.target.closest('[data-slug]');
    if (card) { navigate(`/${card.getAttribute('data-slug')}/overview`); return; }
    const act = event.target.closest('[data-act]');
    if (!act) return;
    if (act.getAttribute('data-act') === 'create') {
      import('./team-create.js').then((mod) => mod.openCreateTeamPage(() => renderTeams(app)));
    }
  });
}

export function renderNoTeam(app) {
  document.title = 'TeamFlow';
  app.innerHTML = `<div class="center-wrap">
    <div class="auth-card">
      <div class="auth-brand"><span class="auth-brand__mark"><gh-icon name="git-merge" size="16"></gh-icon></span>TeamFlow</div>
      ${emptyHTML({ icon: 'organization', title: '还没有团队', desc: '创建一个团队，开始规划议题、里程碑与日程。' })}
      <div class="row" style="justify-content:center">
        ${S.user ? '<gh-btn variant="primary" data-act="create">新建团队</gh-btn>' : '<a class="gh-btn gh-btn--primary" href="/login">登录</a>'}
        <gh-btn variant="invisible" data-act="teams">浏览团队</gh-btn>
      </div>
    </div>
  </div>`;
  onView(app, 'click', (event) => {
    const act = event.target.closest('[data-act]');
    if (!act) return;
    const name = act.getAttribute('data-act');
    if (name === 'teams') navigate('/teams');
    if (name === 'create') import('./team-create.js').then((mod) => mod.openCreateTeamPage());
  });
}
