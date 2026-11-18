const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function uuid() {
  return (crypto.randomUUID ? crypto.randomUUID() : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g,
    c => { const r = Math.random() * 16 | 0; return (c === 'x' ? r : (r & 3 | 8)).toString(16); }));
}

async function api(path, options = {}) {
  const opts = { method: options.method || 'GET', credentials: 'same-origin', headers: {} };
  if (options.body !== undefined) {
    if (options.body instanceof FormData) opts.body = options.body;
    else { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(options.body); }
  }
  if (options.idempotencyKey) opts.headers['Idempotency-Key'] = options.idempotencyKey;
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty response */ }
  if (!res.ok) {
    const err = new Error(data.message || `请求失败（${res.status}）`);
    err.code = data.code; err.details = data.details; err.status = res.status;
    throw err;
  }
  return data;
}

let toastTimer;
function toast(message, type = '') {
  const el = $('#toast');
  if (!el) return;
  el.textContent = message;
  el.className = 'toast show ' + type;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast ' + type; }, 3200);
}

function fieldModal(title, fields, okText = '确定') {
  return new Promise(resolve => {
    const modal = $('#modal');
    const card = $('.modal-card', modal);
    card.replaceChildren();
    const h = document.createElement('h3'); h.textContent = title; card.appendChild(h);
    const form = document.createElement('form');
    const values = {};
    fields.forEach(f => {
      if (f.type === 'html') {
        const p = document.createElement('p'); p.className = 'muted'; p.textContent = f.label;
        form.appendChild(p); return;
      }
      const label = document.createElement('label');
      label.className = 'field';
      const cap = document.createElement('span'); cap.textContent = f.label; label.appendChild(cap);
      let input;
      if (f.type === 'textarea') { input = document.createElement('textarea'); input.value = f.value || ''; }
      else if (f.type === 'select') {
        input = document.createElement('select');
        (f.options || []).forEach(o => {
          const opt = document.createElement('option');
          opt.value = o.value; opt.textContent = o.label;
          if (o.value === f.value) opt.selected = true;
          input.appendChild(opt);
        });
      } else { input = document.createElement('input'); input.type = f.type || 'text'; input.value = f.value || ''; }
      input.name = f.name;
      if (f.placeholder) input.placeholder = f.placeholder;
      if (f.required) input.required = true;
      if (f.min !== undefined) input.min = f.min;
      if (f.max !== undefined) input.max = f.max;
      label.appendChild(input); form.appendChild(label);
      values[f.name] = input;
    });
    const actions = document.createElement('div'); actions.className = 'modal-actions';
    const cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'btn secondary'; cancel.textContent = '取消';
    const ok = document.createElement('button'); ok.type = 'submit'; ok.className = 'btn'; ok.textContent = okText;
    actions.append(cancel, ok); form.appendChild(actions); card.appendChild(form);
    modal.hidden = false;
    const first = Object.values(values)[0]; if (first) first.focus();
    const finish = value => { modal.hidden = true; document.removeEventListener('keydown', onKey); resolve(value); };
    const onKey = e => { if (e.key === 'Escape') finish(null); };
    document.addEventListener('keydown', onKey);
    cancel.onclick = () => finish(null);
    modal.onclick = e => { if (e.target === modal) finish(null); };
    form.onsubmit = e => {
      e.preventDefault();
      const result = {};
      Object.entries(values).forEach(([k, el]) => { result[k] = el.value.trim(); });
      finish(result);
    };
  });
}

function confirmDialog(title, text, okText = '确定') {
  return fieldModal(title, [{ name: '_confirm', type: 'html', label: text }], okText).then(v => !!v);
}

function busy(btn, on) {
  if (!btn) return;
  btn.disabled = on;
  if (on) btn.dataset.label = btn.textContent;
  if (on) btn.textContent = '处理中…';
  else if (btn.dataset.label) btn.textContent = btn.dataset.label;
}

function go(path) { window.location.href = path; }

function readFileAsBytes(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(new Uint8Array(reader.result));
    reader.onerror = reject;
    reader.readAsArrayBuffer(file);
  });
}

document.addEventListener('DOMContentLoaded', () => {
  const bar = $('#progress');
  const onScroll = () => {
    const h = document.documentElement.scrollHeight - window.innerHeight;
    if (bar) bar.style.width = (h > 0 ? window.scrollY / h * 100 : 0) + '%';
  };
  window.addEventListener('scroll', onScroll, { passive: true }); onScroll();

  $$('[data-logout]').forEach(btn => btn.addEventListener('click', async () => {
    try { await api('/api/v1/auth/logout', { method: 'POST' }); toast('已退出登录', 'ok'); setTimeout(() => go('/'), 400); }
    catch (e) { toast(e.message, 'err'); }
  }));

  $$('[data-read-notice]').forEach(el => el.addEventListener('click', async () => {
    try { await api(`/api/v1/notifications/${el.dataset.readNotice}/read`, { method: 'POST' }); go('/notifications'); }
    catch (e) { toast(e.message, 'err'); }
  }));

  const path = window.location.pathname;
  $$('.main-nav a').forEach(a => {
    if (a.getAttribute('href') === path || (path.startsWith('/admin') && a.getAttribute('href') === '/admin')) a.classList.add('active');
  });
});
