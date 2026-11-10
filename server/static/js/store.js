/* TeamFlow · 全局状态与小工具 */

import { api } from './api.js';

const ACTOR_KEY = 'tf:actor';
const THEME_KEY = 'tf:theme';

export const S = {
  boot: null,
  user: null,
  teams: [],
  team: null,
  detail: null,
  role: '',
  canEdit: false,
  requireLogin: false,
  editProtected: false,
  editLocked: false,
  demoAccounts: [],
  lastTeamId: null,
  details: new Map(),
};

/* ---------- 操作身份（未开启登录时用于区分"我"是谁） ---------- */

export function actorId() {
  try { return localStorage.getItem(ACTOR_KEY) || ''; } catch { return ''; }
}

export function setActorId(value) {
  try {
    if (value) localStorage.setItem(ACTOR_KEY, String(value));
    else localStorage.removeItem(ACTOR_KEY);
  } catch { /* 忽略存储异常 */ }
}

export function actorValue() { return actorId() || 'guest'; }

export function actorMember() {
  const id = actorId();
  if (!id) return null;
  return members().find((m) => String(m.id) === String(id)) || null;
}

/* ---------- 主题 ---------- */

export function storedTheme() {
  try { return localStorage.getItem(THEME_KEY) || 'light'; } catch { return 'light'; }
}

export function applyTheme(theme) {
  const value = theme === 'dark' || theme === 'light' ? theme : 'light';
  if (window.GHUI && typeof GHUI.setTheme === 'function') GHUI.setTheme(value);
  else document.documentElement.setAttribute('data-gh-theme', value);
  try { localStorage.setItem(THEME_KEY, value); } catch { /* 忽略 */ }
  return value;
}

export function toggleTheme() {
  const next = (document.documentElement.getAttribute('data-gh-theme') === 'dark') ? 'light' : 'dark';
  return applyTheme(next);
}

export function currentTheme() {
  return document.documentElement.getAttribute('data-gh-theme') === 'dark' ? 'dark' : 'light';
}

/* ---------- 数据加载 ---------- */

export async function bootstrap() {
  const data = await api.get('/api/bootstrap');
  S.boot = data;
  S.user = data.user || null;
  S.teams = data.teams || [];
  S.requireLogin = !!data.require_login;
  S.editProtected = !!data.edit_protected;
  S.editLocked = !!data.edit_locked;
  S.demoAccounts = data.demo_accounts || [];
  S.demoPassword = data.demo_password || '';
  S.lastTeamId = data.last_team_id;
  return data;
}

export function teamBySlug(slug) {
  return S.teams.find((t) => t.slug === slug) || null;
}

export function teamById(id) {
  return S.teams.find((t) => String(t.id) === String(id)) || null;
}

export function pickDefaultTeam() {
  return teamById(S.lastTeamId) || S.teams[0] || null;
}

export async function loadTeam(slug, options = {}) {
  const { force = false, ttl = 4000 } = options;
  const cached = S.details.get(slug);
  if (!force && cached && Date.now() - cached.at < ttl) {
    S.detail = cached.detail;
    S.team = cached.detail;
    S.role = cached.detail.role || '';
    S.canEdit = !!cached.detail.can_edit;
    return cached.detail;
  }
  const data = await api.get(`/api/teams/${encodeURIComponent(slug)}`);
  const detail = data.team;
  S.details.set(slug, { detail, at: Date.now() });
  S.detail = detail;
  S.team = detail;
  S.role = detail.role || '';
  S.canEdit = !!detail.can_edit;
  const index = S.teams.findIndex((t) => t.id === detail.id);
  if (index >= 0) S.teams[index] = { ...S.teams[index], ...detail };
  else S.teams.push(detail);
  return detail;
}

export function invalidateTeam(slug) {
  if (slug) S.details.delete(slug);
  else S.details.clear();
}

export function members() {
  return (S.detail && Array.isArray(S.detail.members)) ? S.detail.members : [];
}

export function memberById(id) {
  return members().find((m) => String(m.id) === String(id)) || null;
}

export function memberLabel(id) {
  const m = memberById(id);
  return m ? (m.display_name || m.username) : '';
}

/* ---------- 路径 ---------- */

export function teamUrl(view = 'overview', slug) {
  const target = slug || (S.detail && S.detail.slug) || (S.team && S.team.slug);
  if (!target) return '/';
  return view ? `/${encodeURIComponent(target)}/${view}` : `/${encodeURIComponent(target)}`;
}

export function issueUrl(number) {
  return `${teamUrl('issues')}/${number}`;
}
