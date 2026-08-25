/* ── Theme ──────────────────────────────────────────────── */
const THEME_KEY = 'brisa-theme';

function initTheme() {
  const saved = localStorage.getItem(THEME_KEY) || 'dark';
  document.documentElement.setAttribute('data-theme', saved);
  updateThemeButton(saved);
}

function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme') || 'dark';
  const next = current === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem(THEME_KEY, next);
  updateThemeButton(next);
}

function updateThemeButton(theme) {
  const btn = document.getElementById('theme-toggle');
  if (!btn) return;
  btn.textContent = theme === 'dark' ? '☀ Light' : '☾ Dark';
}

/* ── Navigation ─────────────────────────────────────────── */
function setActiveNav() {
  const path = window.location.pathname.replace(/\/$/, '') || '/';
  document.querySelectorAll('.nav-item').forEach(el => {
    const href = el.getAttribute('href')?.replace(/\/$/, '') || '';
    el.classList.toggle('active', href === path);
  });
}

/* ── Toast ──────────────────────────────────────────────── */
let toastTimer = null;

function showToast(msg, type = 'ok') {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = msg;
  el.className = `show toast-${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = ''; }, type === 'error' ? 8000 : 3000);
}

/* ── API helpers ────────────────────────────────────────── */
const WRITE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
let authSession = {
  authEnabled: false,
  authenticated: false,
  username: null,
  csrfToken: null,
  version: '',
};
let redirectingToLogin = false;

function normalizeAuthState(payload) {
  return {
    authEnabled: payload?.auth_enabled === true,
    authenticated: payload?.authenticated === true,
    username: typeof payload?.username === 'string' ? payload.username : null,
    csrfToken: typeof payload?.csrf_token === 'string' ? payload.csrf_token : null,
    version: typeof payload?.version === 'string' ? payload.version : '',
  };
}

function apiRequestOptions(method, body = null, csrfToken = null) {
  const normalizedMethod = String(method).toUpperCase();
  const headers = { Accept: 'application/json' };
  const options = {
    method: normalizedMethod,
    headers,
    credentials: 'same-origin',
  };
  if (body !== null) {
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  if (WRITE_METHODS.has(normalizedMethod) && csrfToken) {
    headers['X-CSRF-Token'] = csrfToken;
  }
  return options;
}

function redirectToLogin(locationObject) {
  if (redirectingToLogin) return false;
  redirectingToLogin = true;
  authSession.username = null;
  authSession.csrfToken = null;
  locationObject.replace('/login');
  return true;
}

async function responseError(response, fallback) {
  const payload = await response.json().catch(() => null);
  const detail = payload?.detail;
  const message = Array.isArray(detail) ? detail.join('; ') : detail;
  return new Error(typeof message === 'string' && message ? message : fallback);
}

async function bootstrapAuth(fetchImpl, locationObject) {
  const response = await fetchImpl('/api/auth/me', apiRequestOptions('GET'));
  if (response.status === 401) {
    redirectToLogin(locationObject);
    throw new Error('Authentication required');
  }
  if (!response.ok) {
    throw await responseError(response, 'Unable to initialize authentication');
  }

  authSession = normalizeAuthState(await response.json());
  if (authSession.authEnabled && !authSession.authenticated) {
    redirectToLogin(locationObject);
    throw new Error('Authentication required');
  }
  return authSession;
}

const authReady = typeof window !== 'undefined'
  ? bootstrapAuth(window.fetch.bind(window), window.location)
  : Promise.resolve(authSession);
authReady.catch(() => {});

async function api(method, path, body = null) {
  await authReady;
  if (redirectingToLogin) throw new Error('Authentication required');

  const opts = apiRequestOptions(method, body, authSession.csrfToken);
  const res = await fetch(`/api${path}`, opts);
  if (res.status === 401) {
    redirectToLogin(window.location);
    throw new Error('Authentication required');
  }
  if (!res.ok) {
    throw await responseError(res, res.statusText || 'Request failed');
  }
  if (res.status === 204) return null;
  return res.json();
}

async function logout() {
  document.querySelectorAll('[data-logout]').forEach(button => {
    button.disabled = true;
  });
  try {
    await api('POST', '/auth/logout');
    redirectToLogin(window.location);
  } catch (error) {
    if (!redirectingToLogin) {
      document.querySelectorAll('[data-logout]').forEach(button => {
        button.disabled = false;
      });
      showToast('Unable to log out. Please try again.', 'error');
    }
  }
}

/* ── Value flash animation ──────────────────────────────── */
function flashValue(el, newText) {
  if (!el) return;
  if (el.textContent === newText) return;
  el.textContent = newText;
  el.classList.remove('flash');
  void el.offsetWidth; // reflow
  el.classList.add('flash');
  setTimeout(() => el.classList.remove('flash'), 600);
}

/* ── Format helpers ─────────────────────────────────────── */
function fmtTemp(val) {
  return val != null ? val.toFixed(1) : '—';
}

function fmtRpm(val) {
  return val != null ? Math.round(val).toString() : '—';
}

function fmtPercent(val) {
  return val != null ? val.toString() : '—';
}

function relativeTime(ts) {
  const diff = Math.floor(Date.now() / 1000) - ts;
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  return `${Math.floor(diff / 3600)}h ago`;
}

/* ── Sensor reference helpers ───────────────────────────── */
function sensorReferenceKind(sensorId, config) {
  const configuredVirtual = (config.virtual_sensors || [])
    .some(vs => vs.id === sensorId);
  return configuredVirtual || sensorId.startsWith('virtual/')
    ? 'virtual'
    : 'physical';
}

function configuredPhysicalSensorIds(config) {
  const ids = [];
  const seen = new Set();

  function add(sensorId) {
    if (!sensorId || seen.has(sensorId) ||
        sensorReferenceKind(sensorId, config) !== 'physical') return;
    seen.add(sensorId);
    ids.push(sensorId);
  }

  (config.virtual_sensors || []).forEach(vs =>
    (vs.source_sensor_ids || []).forEach(add)
  );
  (config.fan_configs || []).forEach(fc => add(fc.sensor_id));
  return ids;
}

function buildPhysicalSensorChoices(detectedSensors, retainedIds, config) {
  const choices = [];
  const seen = new Set();
  const aliases = config.sensor_aliases || {};

  (detectedSensors || []).forEach(sensor => {
    seen.add(sensor.id);
    choices.push({
      id: sensor.id,
      label: sensor.alias || aliases[sensor.id] || sensor.label,
      unavailable: false,
    });
  });

  (retainedIds || []).forEach(sensorId => {
    if (seen.has(sensorId) || sensorReferenceKind(sensorId, config) !== 'physical') return;
    seen.add(sensorId);
    choices.push({
      id: sensorId,
      label: aliases[sensorId] || sensorId,
      unavailable: true,
    });
  });

  return choices;
}

function virtualSensorDependencyMessage(virtualSensor, fanConfigs) {
  const usedBy = (fanConfigs || [])
    .filter(fc => fc.sensor_id === virtualSensor.id);
  if (!usedBy.length) return null;

  const fanNames = usedBy.map(fc => `- ${fc.fan_label || fc.fan_id}`);
  return `Cannot delete "${virtualSensor.name}".\n` +
    `It is currently used by:\n${fanNames.join('\n')}\n\n` +
    'Reassign those fan configurations first in Fan Config.';
}

/* ── Sidebar HTML (injected into each page) ─────────────── */
const SIDEBAR_HTML = `
<div class="sidebar-logo">
  <div style="display:flex; align-items:center; gap:10px;">
    <img src="/logo.png" alt="Brisa" style="width:48px; height:48px; object-fit:contain; flex-shrink:0;" />
    <div>
      <div class="wordmark">bri<span>sa</span></div>
      <div class="version" id="app-version"></div>
    </div>
  </div>
</div>
<nav class="nav">
  <a class="nav-item" href="/">
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">
      <rect x="1" y="1" width="6" height="6" rx="1"/>
      <rect x="9" y="1" width="6" height="6" rx="1"/>
      <rect x="1" y="9" width="6" height="6" rx="1"/>
      <rect x="9" y="9" width="6" height="6" rx="1"/>
    </svg>
    Dashboard
  </a>
  <a class="nav-item" href="/devices.html">
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">
      <circle cx="8" cy="8" r="6"/>
      <circle cx="8" cy="8" r="2"/>
      <line x1="8" y1="2" x2="8" y2="4"/>
      <line x1="8" y1="12" x2="8" y2="14"/>
      <line x1="2" y1="8" x2="4" y2="8"/>
      <line x1="12" y1="8" x2="14" y2="8"/>
    </svg>
    Sensors & Fans
  </a>
  <a class="nav-item" href="/curves.html">
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">
      <polyline points="1,13 4,9 7,10 10,5 15,2"/>
    </svg>
    Curves
  </a>
  <a class="nav-item" href="/fanconfig.html">
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">
      <circle cx="8" cy="8" r="2.5"/>
      <path d="M8 1 C8 1 10 3 10 5 C10 6.5 9 7.5 8 8"/>
      <path d="M15 8 C15 8 13 10 11 10 C9.5 10 8.5 9 8 8"/>
      <path d="M8 15 C8 15 6 13 6 11 C6 9.5 7 8.5 8 8"/>
      <path d="M1 8 C1 8 3 6 5 6 C6.5 6 7.5 7 8 8"/>
    </svg>
    Fan Config
  </a>
  <a class="nav-item" href="/history.html">
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">
      <polyline points="1,12 5,7 8,9 12,4 15,6"/>
      <line x1="1" y1="15" x2="15" y2="15"/>
    </svg>
    History
  </a>
  <a class="nav-item" href="/settings.html">
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">
      <circle cx="8" cy="8" r="2.5"/>
      <path d="M8 1v2M8 13v2M1 8h2M13 8h2M3.05 3.05l1.41 1.41M11.54 11.54l1.41 1.41M3.05 12.95l1.41-1.41M11.54 4.46l1.41-1.41"/>
    </svg>
    Settings
  </a>
</nav>
<div class="sidebar-footer">
  <div class="sidebar-auth" id="sidebar-auth" hidden>
    <span class="auth-username" id="auth-username"></span>
    <button class="logout-button" type="button" data-logout>Log out</button>
  </div>
  <div class="sidebar-tools">
    <button class="theme-toggle" id="theme-toggle" type="button">☀ Light</button>
    <a class="github-link" href="https://github.com/HarrenTheBlack/brisa" target="_blank" rel="noopener" title="GitHub">
      <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>
    </a>
  </div>
</div>
`;

function displayVersion(version) {
  if (!version) return '';
  return version.startsWith('v') ? version : `v${version}`;
}

function renderAuthChrome(state) {
  const version = document.getElementById('app-version');
  if (version) version.textContent = displayVersion(state.version);
  if (!state.authEnabled || !state.authenticated) return;

  const sidebarAuth = document.getElementById('sidebar-auth');
  const username = document.getElementById('auth-username');
  if (sidebarAuth) sidebarAuth.hidden = false;
  if (username) username.textContent = state.username || '';
  document.body.classList.add('auth-active');

  const mobileAuth = document.createElement('div');
  mobileAuth.className = 'mobile-auth';
  const mobileUsername = document.createElement('span');
  mobileUsername.className = 'auth-username';
  mobileUsername.textContent = state.username || '';
  const mobileLogout = document.createElement('button');
  mobileLogout.className = 'logout-button';
  mobileLogout.type = 'button';
  mobileLogout.dataset.logout = '';
  mobileLogout.textContent = 'Log out';
  mobileAuth.append(mobileUsername, mobileLogout);
  document.body.appendChild(mobileAuth);

  document.querySelectorAll('[data-logout]').forEach(button => {
    button.addEventListener('click', logout);
  });
}

function initPage() {
  // Inject sidebar
  const sidebar = document.getElementById('sidebar');
  if (sidebar) sidebar.innerHTML = SIDEBAR_HTML;

  initTheme();
  document.getElementById('theme-toggle')?.addEventListener('click', toggleTheme);
  setActiveNav();
  authReady.then(renderAuthChrome).catch(() => {});
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    sensorReferenceKind,
    configuredPhysicalSensorIds,
    buildPhysicalSensorChoices,
    virtualSensorDependencyMessage,
    normalizeAuthState,
    apiRequestOptions,
    displayVersion,
  };
}

if (typeof document !== 'undefined') {
  document.addEventListener('DOMContentLoaded', initPage);
}
