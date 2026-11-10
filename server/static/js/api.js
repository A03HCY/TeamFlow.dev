/* TeamFlow · 后端接口封装（fetch + 错误翻译 + 编辑口令/登录失效处理） */

export class ApiError extends Error {
  constructor(message, status = 0, code = '') {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

let actorSource = () => '';
let lockHandler = null;
let unauthorizedHandler = null;

export function setActorSource(fn) { actorSource = typeof fn === 'function' ? fn : () => ''; }
export function setLockHandler(fn) { lockHandler = fn; }
export function setUnauthorizedHandler(fn) { unauthorizedHandler = fn; }

export function toQuery(params) {
  if (!params) return '';
  const sp = new URLSearchParams();
  Object.keys(params).forEach((key) => {
    const value = params[key];
    if (value === undefined || value === null || value === '') return;
    sp.set(key, String(value));
  });
  const text = sp.toString();
  return text ? `?${text}` : '';
}

async function send(path, options = {}) {
  const { method = 'GET', body, form, retry = true } = options;
  const headers = {};
  const actor = actorSource();
  if (actor) headers['X-Actor-Id'] = String(actor);
  let payload;
  if (form) {
    payload = form;
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, { method, headers, body: payload, credentials: 'same-origin', cache: 'no-store' });
  } catch {
    throw new ApiError('无法连接服务器，请确认服务正在运行', 0, 'network');
  }
  const type = res.headers.get('content-type') || '';
  let data = null;
  if (/json/i.test(type)) {
    try { data = await res.json(); } catch { data = null; }
  }
  if (res.ok) return data;

  const code = (data && data.code) || '';
  const message = (data && (data.error || data.detail)) || `请求失败（HTTP ${res.status}）`;

  if (code === 'edit_locked' && retry && lockHandler) {
    const unlocked = await lockHandler();
    if (unlocked) return send(path, { method, body, form, retry: false });
  }
  if (res.status === 401 && retry && !code && unauthorizedHandler) unauthorizedHandler(message);
  throw new ApiError(String(message), res.status, code);
}

export const api = {
  get: (path, params) => send(path + toQuery(params)),
  post: (path, body) => send(path, { method: 'POST', body }),
  patch: (path, body) => send(path, { method: 'PATCH', body }),
  del: (path) => send(path, { method: 'DELETE' }),
  upload: (path, form) => send(path, { method: 'POST', form }),
};
