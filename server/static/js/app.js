/* TeamFlow · 应用入口：路由分发、全局交互与状态修复 */

import { api, setActorSource, setLockHandler, setUnauthorizedHandler } from './api.js';
import {
  S, actorId, applyTheme, storedTheme, bootstrap, loadTeam,
  teamBySlug, pickDefaultTeam, invalidateTeam,
} from './store.js';
import { setHandler, start, navigate } from './router.js';
import { renderShell, refreshShell, VIEW_TITLES } from './shell.js';
import { esc, SPINNER, emptyHTML, toast, askText, openDialog, hydrateFormValues } from './ui.js';
import { mountAiDock, setDockContext } from './ai-dock.js';

import * as auth from './pages/auth.js';
import * as ai from './pages/ai.js';
import * as overview from './pages/overview.js';
import * as issues from './pages/issues.js';
import * as issue from './pages/issue.js';
import * as board from './pages/board.js';
import * as week from './pages/week.js';
import * as milestones from './pages/milestones.js';
import * as roadmap from './pages/roadmap.js';
import * as schedule from './pages/schedule.js';
import * as engine from './pages/engine.js';
import * as settings from './pages/settings.js';
import * as projectNew from './pages/project-new.js';
import * as policy from './pages/policy.js';

const PAGES = {
  ai, overview, issues, issue, board, week, milestones, roadmap, schedule, engine, policy, settings,
};

function fatalHTML(message, hint = '') {
  return `<div class="center-wrap"><div class="auth-card">
    <div class="auth-brand"><span class="auth-brand__mark"><gh-icon name="alert" size="16"></gh-icon></span>TeamFlow</div>
    <p class="error-box__msg">${esc(message || '页面无法加载')}</p>
    ${hint ? `<p class="error-box__msg">地址：${esc(hint)}</p>` : ''}
    <div class="row">
      <gh-btn data-act="home">返回首页</gh-btn>
      <gh-btn variant="invisible" data-act="teams">全部团队</gh-btn>
    </div>
  </div></div>`;
}

function bindFatalActions(root) {
  root.addEventListener('click', (event) => {
    const act = event.target.closest('[data-act]');
    if (!act) return;
    const name = act.getAttribute('data-act');
    if (name === 'home') location.href = '/';
    if (name === 'teams') navigate('/teams');
  });
}

async function onRoute(route) {
  window.__tfRoute = route;
  const app = document.getElementById('app');
  if (!app) return;
  setDockContext(route);

  try {
    if (!S.boot) await bootstrap();
  } catch (error) {
    app.innerHTML = fatalHTML(error && error.message ? error.message : '无法加载基础数据');
    bindFatalActions(app);
    return;
  }

  if (route.kind === 'login') { auth.renderLogin(app); return; }
  if (route.kind === 'teams-new') { projectNew.render(app, route); return; }
  if (route.kind === 'teams') { auth.renderTeams(app); return; }

  if (route.kind === 'home') {
    // 开了登录模式且没有会话：直接去登录页，别停在「还没有团队」的空状态
    if (S.requireLogin && !S.user) { navigate('/login', { replace: true }); return; }
    const team = pickDefaultTeam();
    if (team) { navigate(`/${team.slug}/overview`, { replace: true }); return; }
    auth.renderNoTeam(app);
    return;
  }

  if (route.kind !== 'team') {
    app.innerHTML = fatalHTML('页面不存在', route.path);
    bindFatalActions(app);
    return;
  }

  if (!PAGES[route.view]) {
    navigate(`/${route.slug}/overview`, { replace: true });
    return;
  }

  let team = teamBySlug(route.slug);
  if (!team) {
    try {
      const data = await api.get(`/api/teams/${encodeURIComponent(route.slug)}`);
      team = data.team;
      S.teams.push(team);
    } catch (error) {
      app.innerHTML = fatalHTML((error && error.message) || '团队不存在', `/${route.slug}`);
      bindFatalActions(app);
      return;
    }
  }

  try {
    await loadTeam(team.slug);
  } catch (error) {
    app.innerHTML = fatalHTML((error && error.message) || '无法访问该团队', `/${route.slug}`);
    bindFatalActions(app);
    return;
  }

  const view = renderShell(route);
  if (!view) return;
  view.innerHTML = SPINNER;
  const page = (route.view === 'issues' && route.arg) ? issue : PAGES[route.view];
  try {
    await page.render(view, route);
  } catch (error) {
    console.error('[page]', error);
    view.innerHTML = `<div class="error-box">
      <gh-icon name="alert" size="24"></gh-icon>
      <div class="error-box__title">页面加载失败</div>
      <div class="error-box__msg">${esc((error && error.message) || '未知错误')}</div>
      <gh-btn size="sm" data-act="reload">重新加载</gh-btn>
    </div>`;
    view.addEventListener('click', (event) => {
      if (event.target.closest('[data-act="reload"]')) location.reload();
    });
  }
  document.title = `${(S.detail && S.detail.name) || 'TeamFlow'} · ${VIEW_TITLES[route.view] || ''}`.trim();
  refreshShell(route);
  document.getElementById('boot')?.remove();
}

/* ---------------- 全局配置 ---------------- */

setActorSource(() => actorId());

setLockHandler(async () => {
  const password = await askText({
    title: '需要编辑口令',
    label: '该操作会修改数据，请输入编辑口令',
    okText: '解锁',
  });
  if (!password) return false;
  try {
    await api.post('/api/edit/unlock', { password });
    S.editLocked = false;
    toast('已解锁，可以继续编辑');
    return true;
  } catch (error) {
    toast((error && error.message) || '口令不正确', 'err');
    return false;
  }
});

setUnauthorizedHandler(() => {
  if (!S.user) return;
  toast('登录状态已失效，请重新登录', 'err');
  S.user = null;
  if (S.requireLogin) { navigate('/login'); return; }
  location.reload();
});

document.addEventListener('click', (event) => {
  if (event.defaultPrevented || event.button !== 0) return;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const link = event.target.closest('a[href]');
  if (!link) return;
  const href = link.getAttribute('href');
  if (!href || link.target === '_blank' || link.hasAttribute('download')) return;
  if (/^(https?:|mailto:|tel:|#)/i.test(href) || href.startsWith('//')) return;
  if (!href.startsWith('/')) return;
  event.preventDefault();
  navigate(href);
});

/* gh-textarea 不认 value 属性：元素插入后统一补值（页面与弹层都覆盖） */
const fieldObserver = new MutationObserver((records) => {
  records.forEach((record) => {
    record.addedNodes.forEach((node) => {
      if (node.nodeType !== 1) return;
      if (node.matches?.('gh-textarea[value]')) {
        if (node.dataset.tfHydrated !== '1') {
          node.dataset.tfHydrated = '1';
          if ('value' in node) node.value = node.getAttribute('value') || '';
        }
      }
      hydrateFormValues(node);
    });
  });
});
fieldObserver.observe(document.body, { childList: true, subtree: true });

async function boot() {
  applyTheme(storedTheme());
  hydrateFormValues(document.body);
  mountAiDock();
  setHandler(onRoute);
  try {
    await start();
  } finally {
    document.getElementById('boot')?.remove();
  }
}

boot();

export { navigate };
