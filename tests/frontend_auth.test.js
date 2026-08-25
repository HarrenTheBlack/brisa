const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const STATIC_ROOT = path.join(__dirname, '..', 'brisa', 'app', 'static');
const APP_SOURCE = fs.readFileSync(path.join(STATIC_ROOT, 'app.js'), 'utf8');
const LOGIN_SOURCE = fs.readFileSync(path.join(STATIC_ROOT, 'login.js'), 'utf8');
const STATIC_SOURCES = fs.readdirSync(STATIC_ROOT)
  .filter(name => name.endsWith('.html') || name.endsWith('.js'))
  .map(name => fs.readFileSync(path.join(STATIC_ROOT, name), 'utf8'))
  .join('\n');

const {
  apiRequestOptions,
  displayVersion,
  normalizeAuthState,
} = require('../brisa/app/static/app.js');
const { loginRequestOptions } = require('../brisa/app/static/login.js');


function loadAppInternals() {
  const sandbox = {
    clearTimeout,
    console,
    module: { exports: {} },
    setTimeout,
  };
  vm.runInNewContext(`${APP_SOURCE}\nObject.assign(module.exports, {
    api, bootstrapAuth, logout, redirectToLogin,
    setAuthSessionForTest: value => { authSession = value; },
  });`, sandbox);
  return { ...sandbox.module.exports, sandbox };
}


function loadLoginInternals() {
  const sandbox = { module: { exports: {} } };
  vm.runInNewContext(`${LOGIN_SOURCE}\nmodule.exports.redirectIfAlreadyAuthenticated = redirectIfAlreadyAuthenticated;`, sandbox);
  return sandbox.module.exports;
}


test('login sends credentials only in a same-origin JSON request body', () => {
  const username = 'admin@example.test?next=https://evil.test';
  const password = 'p@ss#word&token=secret';
  const options = loginRequestOptions(username, password);

  assert.equal(options.method, 'POST');
  assert.equal(options.credentials, 'same-origin');
  assert.deepEqual(options.headers, {
    Accept: 'application/json',
    'Content-Type': 'application/json',
  });
  assert.deepEqual(JSON.parse(options.body), { username, password });
  assert.match(LOGIN_SOURCE, /fetch\(\s*['"]\/api\/auth\/login['"]/);
  assert.doesNotMatch(LOGIN_SOURCE, /URLSearchParams|[?&](?:username|password)=|encodeURIComponent\((?:username|password)/);
});


test('normalizes auth bootstrap data and ignores malformed fields', () => {
  const state = normalizeAuthState({
    auth_enabled: true,
    authenticated: true,
    username: 'operator',
    csrf_token: 'csrf-value',
    version: '2.4.0',
    password: 'must-not-survive',
  });
  assert.deepEqual({ ...state }, {
    authEnabled: true,
    authenticated: true,
    username: 'operator',
    csrfToken: 'csrf-value',
    version: '2.4.0',
  });
  assert.deepEqual({ ...normalizeAuthState({
    auth_enabled: 1,
    authenticated: 'yes',
    username: 42,
    csrf_token: {},
    version: null,
  }) }, {
    authEnabled: false,
    authenticated: false,
    username: null,
    csrfToken: null,
    version: '',
  });
});


test('auth bootstrap requests /me with same-origin GET options', async () => {
  const { bootstrapAuth } = loadAppInternals();
  const calls = [];
  const response = {
    ok: true,
    status: 200,
    json: async () => ({ auth_enabled: true, authenticated: true, username: 'admin' }),
  };
  const state = await bootstrapAuth(async (...args) => {
    calls.push(args);
    return response;
  }, { replace() { assert.fail('must not redirect'); } });

  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], '/api/auth/me');
  assert.equal(calls[0][1].method, 'GET');
  assert.equal(calls[0][1].credentials, 'same-origin');
  assert.deepEqual({ ...calls[0][1].headers }, { Accept: 'application/json' });
  assert.equal(state.authenticated, true);
});


test('CSRF is attached to unsafe methods but never GET', () => {
  for (const method of ['POST', 'PUT', 'PATCH', 'DELETE']) {
    const options = apiRequestOptions(method, { enabled: true }, 'csrf-value');
    assert.equal(options.headers['X-CSRF-Token'], 'csrf-value', method);
    assert.equal(options.credentials, 'same-origin');
    assert.equal(options.body, '{"enabled":true}');
  }

  const get = apiRequestOptions('get', null, 'csrf-value');
  assert.equal(get.method, 'GET');
  assert.equal(get.headers['X-CSRF-Token'], undefined);
  assert.equal(get.body, undefined);
});


test('repeated API calls after a 401 fetch once and redirect once', async () => {
  const internals = loadAppInternals();
  let fetches = 0;
  let redirects = 0;
  internals.setAuthSessionForTest({
    authEnabled: true,
    authenticated: true,
    username: 'admin',
    csrfToken: 'csrf-value',
    version: '2.0.0',
  });
  internals.sandbox.fetch = async () => {
    fetches += 1;
    return { ok: false, status: 401 };
  };
  internals.sandbox.window = {
    location: { replace(pathname) {
      redirects += 1;
      assert.equal(pathname, '/login');
    } },
  };

  await assert.rejects(internals.api('GET', '/state'), /Authentication required/);
  await assert.rejects(internals.api('GET', '/state'), /Authentication required/);
  assert.equal(fetches, 1);
  assert.equal(redirects, 1);
});


test('logout posts with CSRF, disables desktop/mobile controls, and redirects', async () => {
  const internals = loadAppInternals();
  const buttons = [{ disabled: false }, { disabled: false }];
  const requests = [];
  let redirectedTo = null;
  internals.setAuthSessionForTest({
    authEnabled: true,
    authenticated: true,
    username: 'admin',
    csrfToken: 'csrf-value',
    version: '2.0.0',
  });
  internals.sandbox.document = {
    getElementById: () => null,
    querySelectorAll: selector => {
      assert.equal(selector, '[data-logout]');
      return buttons;
    },
  };
  internals.sandbox.fetch = async (...args) => {
    requests.push(args);
    return { ok: true, status: 204 };
  };
  internals.sandbox.window = {
    location: { replace: pathname => { redirectedTo = pathname; } },
  };

  await internals.logout();
  assert.equal(requests.length, 1);
  assert.equal(requests[0][0], '/api/auth/logout');
  assert.equal(requests[0][1].method, 'POST');
  assert.equal(requests[0][1].headers['X-CSRF-Token'], 'csrf-value');
  assert.deepEqual(buttons.map(button => button.disabled), [true, true]);
  assert.equal(redirectedTo, '/login');
});


test('login-page bootstrap uses same-origin GET and redirects authenticated users', async () => {
  const { redirectIfAlreadyAuthenticated } = loadLoginInternals();
  const calls = [];
  let redirectedTo = null;
  const didRedirect = await redirectIfAlreadyAuthenticated(async (...args) => {
    calls.push(args);
    return {
      ok: true,
      json: async () => ({ auth_enabled: true, authenticated: true }),
    };
  }, { replace: pathname => { redirectedTo = pathname; } });

  assert.equal(didRedirect, true);
  assert.equal(calls[0][0], '/api/auth/me');
  assert.equal(calls[0][1].method, 'GET');
  assert.equal(calls[0][1].credentials, 'same-origin');
  assert.equal(redirectedTo, '/');
});


test('version comes from bootstrap state and is not hardcoded to 1.0.1', () => {
  assert.equal(displayVersion('2.7.3'), 'v2.7.3');
  assert.equal(displayVersion('v3.0.0'), 'v3.0.0');
  assert.equal(displayVersion(''), '');
  assert.doesNotMatch(STATIC_SOURCES, /\b1\.0\.1\b/);
  assert.match(APP_SOURCE, /version\.textContent = displayVersion\(state\.version\)/);
});


test('browser storage is used only for the non-secret theme preference', () => {
  assert.doesNotMatch(STATIC_SOURCES, /sessionStorage|indexedDB|IndexedDB/);
  assert.doesNotMatch(STATIC_SOURCES, /(?:localStorage|sessionStorage)\.(?:setItem|getItem)\([^\n]*(?:password|csrf|token|username|credential)/i);

  const localStorageCalls = [...STATIC_SOURCES.matchAll(/localStorage\.(?:getItem|setItem)\(([^,\n)]+)/g)]
    .map(match => match[1].trim());
  assert.ok(localStorageCalls.length > 0);
  assert.ok(localStorageCalls.every(key => key === 'THEME_KEY' || key === "'brisa-theme'"));
});


test('desktop and mobile logout controls share the same data attribute handler', () => {
  assert.match(APP_SOURCE, /<button class="logout-button" type="button" data-logout>Log out<\/button>/);
  assert.match(APP_SOURCE, /mobileLogout\.dataset\.logout = '';/);
  assert.match(APP_SOURCE, /querySelectorAll\('\[data-logout\]'\)/);
  assert.match(APP_SOURCE, /button\.addEventListener\('click', logout\)/);
});
