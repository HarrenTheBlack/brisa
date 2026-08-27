'use strict';

function loginRequestOptions(username, password) {
  return {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ username, password }),
  };
}

function setLoginBusy(form, button, busy) {
  form.setAttribute('aria-busy', String(busy));
  button.disabled = busy;
  button.textContent = busy ? 'Signing in…' : 'Sign in';
}

function togglePasswordVisibility(password, toggle) {
  const visible = password.type === 'password';
  password.type = visible ? 'text' : 'password';
  toggle.setAttribute('aria-label', visible ? 'Hide password' : 'Show password');
  toggle.setAttribute('aria-pressed', String(visible));
  toggle.classList.toggle('is-visible', visible);
  return visible;
}

async function redirectIfAlreadyAuthenticated(fetchImpl, locationObject) {
  try {
    const response = await fetchImpl('/api/auth/me', {
      method: 'GET',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    });
    if (!response.ok) return false;
    const state = await response.json();
    if (state.auth_enabled === false || state.authenticated === true) {
      locationObject.replace('/');
      return true;
    }
  } catch (_) {
    // The form remains available; submission reports a generic failure.
  }
  return false;
}

function initLogin() {
  const savedTheme = localStorage.getItem('brisa-theme') || 'dark';
  document.documentElement.setAttribute('data-theme', savedTheme);

  const form = document.getElementById('login-form');
  const username = document.getElementById('username');
  const password = document.getElementById('password');
  const passwordToggle = document.getElementById('password-toggle');
  const button = document.getElementById('login-submit');
  const error = document.getElementById('login-error');

  redirectIfAlreadyAuthenticated(window.fetch.bind(window), window.location);
  passwordToggle?.addEventListener('click', () => {
    togglePasswordVisibility(password, passwordToggle);
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!username.value || !password.value) {
      error.textContent = 'Unable to sign in. Check your credentials and try again.';
      return;
    }

    error.textContent = '';
    setLoginBusy(form, button, true);
    try {
      const response = await fetch(
        '/api/auth/login',
        loginRequestOptions(username.value, password.value)
      );
      if (response.status === 429) {
        throw new Error('Too many attempts. Wait before trying again.');
      }
      if (response.status === 503) {
        throw new Error('Management interface unavailable.');
      }
      if (!response.ok) throw new Error('Login failed');
      window.location.replace('/');
    } catch (requestError) {
      password.value = '';
      error.textContent = [
        'Management interface unavailable.',
        'Too many attempts. Wait before trying again.',
      ].includes(requestError.message)
        ? requestError.message
        : 'Unable to sign in. Check your credentials and try again.';
      password.focus();
      setLoginBusy(form, button, false);
    }
  });
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { loginRequestOptions, togglePasswordVisibility };
}

if (typeof document !== 'undefined') {
  document.addEventListener('DOMContentLoaded', initLogin);
}
