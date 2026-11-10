/* TeamFlow · 极简 history 路由 */

let handler = null;
let running = false;
let pending = false;

export function setHandler(fn) { handler = typeof fn === 'function' ? fn : null; }

export function parseLocation() {
  const path = location.pathname.replace(/\/+$/, '');
  const params = new URLSearchParams(location.search);
  const segments = path.split('/').filter(Boolean).map((part) => {
    try { return decodeURIComponent(part); } catch { return part; }
  });
  if (!segments.length) return { kind: 'home', params, path };
  if (segments[0] === 'login') return { kind: 'login', params, path };
  if (segments[0] === 'teams') {
    if (segments[1] === 'new') return { kind: 'teams-new', params, path };
    if (!segments[1]) return { kind: 'teams', params, path };
  }
  if (segments[0] === 'api') return { kind: 'unknown', params, path };
  return {
    kind: 'team',
    slug: segments[0],
    view: segments[1] || 'overview',
    arg: segments[2] || null,
    segments,
    params,
    path,
  };
}

async function run() {
  if (!handler) return;
  if (running) { pending = true; return; }
  running = true;
  try {
    await handler(parseLocation());
  } catch (error) {
    console.error('[route]', error);
  } finally {
    running = false;
    if (pending) { pending = false; run(); }
  }
}

export function navigate(target, options = {}) {
  const { replace = false, keepScroll = false } = options;
  let url;
  try { url = new URL(target, location.origin); } catch { return; }
  if (url.origin !== location.origin) { location.href = url.href; return; }
  const next = url.pathname + url.search;
  const current = location.pathname + location.search;
  if (next === current) {
    if (!keepScroll) window.scrollTo({ top: 0 });
    run();
    return;
  }
  if (replace) history.replaceState({ tf: true }, '', next);
  else history.pushState({ tf: true }, '', next);
  if (!keepScroll) window.scrollTo({ top: 0 });
  document.getElementById('boot')?.remove();
  run();
}

export function reload() { return run(); }

export function start() {
  window.addEventListener('popstate', () => { window.scrollTo({ top: 0 }); run(); });
  if (!history.state) history.replaceState({ tf: true }, '', location.pathname + location.search);
  return run();
}
