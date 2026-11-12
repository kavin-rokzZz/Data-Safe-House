/* Shared UI helpers: theme toggle, local time rendering, CSRF fetch, toasts, confirm. */
(function () {
  const root = document.documentElement;
  window.toggleTheme = function () {
    const dark = root.classList.toggle('dark');
    try { localStorage.setItem('theme', dark ? 'dark' : 'light'); } catch (e) {}
  };

  window.csrfToken = () => document.querySelector('meta[name="csrf"]').content;

  window.api = async function (url, opts = {}) {
    const res = await fetch(url, {
      method: opts.method || 'GET',
      headers: Object.assign({ 'X-CSRF-Token': csrfToken() }, opts.headers || {}),
      body: opts.body,
      credentials: 'same-origin',
    });
    if (res.status === 401) { location.href = '/login'; throw new Error('Session expired'); }
    if (!res.ok) {
      let msg = res.statusText;
      try { msg = (await res.json()).detail || msg; } catch (e) {}
      throw new Error(msg);
    }
    return res.headers.get('content-type')?.includes('json') ? res.json() : res;
  };

  window.postForm = (url, data) => api(url, { method: 'POST', body: new URLSearchParams(data) });

  window.toast = function (msg, kind = 'ok') {
    const el = document.createElement('div');
    el.className = 'fixed bottom-4 right-4 z-[100] rounded-lg px-4 py-2 text-sm shadow-lg text-white ' +
      (kind === 'ok' ? 'bg-emerald-600' : 'bg-rose-600');
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 4000);
  };

  window.formatBytes = function (n) {
    if (n == null) return '—';
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : n.toFixed(0)) + ' ' + u[i];
  };

  function localiseTimes(scope) {
    (scope || document).querySelectorAll('time.ts').forEach((t) => {
      const d = new Date(t.getAttribute('datetime'));
      if (!isNaN(d)) t.textContent = d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
    });
  }
  window.localiseTimes = localiseTimes;
  document.addEventListener('DOMContentLoaded', () => localiseTimes());

  // Confirm-before-submit for <form data-confirm="...">
  document.addEventListener('submit', (e) => {
    const msg = e.target.dataset && e.target.dataset.confirm;
    if (msg && !confirm(msg)) e.preventDefault();
  });
})();
