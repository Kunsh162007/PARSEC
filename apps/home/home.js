/*
 * My deals: lists the deal folders as cards and lets the assistant open one ("open Acme", "what's wrong with
 * Acme's customer contract"). Opening happens on the review page, which carries on with the question.
 */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const AS = window.FinePrintAssistant;
  const enc = encodeURIComponent;
  const state = { deals: [], root: '', live: false };

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (k === 'text') node.textContent = v;
      else node.setAttribute(k, v);
    });
    children.filter(Boolean).forEach((c) => node.append(c));
    return node;
  }

  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  // The access code, when this server needs one, kept for this browser tab. A shared link like /?code=XYZ sets it
  // and is then taken out of the address bar, so a visitor never has to type it.
  const CODE_KEY = 'fineprint-access-code';
  function savedCode() {
    try { return sessionStorage.getItem(CODE_KEY) || ''; } catch (err) { return ''; }
  }
  function saveCode(code) {
    try { sessionStorage.setItem(CODE_KEY, code); } catch (err) { /* private mode: asked again next visit */ }
  }
  function forgetCode() {
    try { sessionStorage.removeItem(CODE_KEY); } catch (err) { /* nothing was kept */ }
  }
  function adoptCodeFromLink() {
    const url = new URL(window.location.href);
    const code = (url.searchParams.get('code') || '').trim().slice(0, 200);
    if (!code) return;
    saveCode(code);
    url.searchParams.delete('code');
    history.replaceState(null, '', url.pathname + url.search + url.hash);
  }

  function headers(extra) {
    const out = Object.assign({ Accept: 'application/json' }, extra || {});
    const code = savedCode();
    if (code) out['X-Review-Token'] = code;
    if (window.FinePrintVisitor) out['X-Deal-Owner'] = window.FinePrintVisitor.key();
    return out;
  }

  // Shown in place of an error when the server needs an access code this tab does not have yet.
  function askForCode(wrong) {
    const box = $('deals-error');
    const input = el('input', { type: 'password', class: 'code-input', autocomplete: 'off', 'aria-label': 'Access code' });
    const use = el('button', { type: 'button', class: 'cta', text: 'Open the demo' });
    const go = () => {
      const code = input.value.trim().slice(0, 200);
      if (!code) return;
      saveCode(code);
      box.hidden = true;
      loadDeals();
    };
    use.addEventListener('click', go);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
    $('deals-root').textContent = 'needs an access code';
    box.replaceChildren(
      el('p', { text: wrong ? 'That access code was not accepted. Check it and try again.' : 'This demo needs an access code. Enter the one you were given, or open the link you were sent.' }),
      el('div', { class: 'code-row' }, input, use));
    box.hidden = false;
    input.focus();
  }
  const dealUrl = (name, ask) => `/review/?deal=${enc(name)}${ask ? `&ask=${enc(ask)}` : ''}`;

  async function removeDeal(name, button) {
    if (!window.confirm(`Remove "${name}"? Its documents, review and marked copies are deleted from this site.`)) return;
    button.disabled = true;
    try {
      const res = await fetch(`/api/v1/deals/${enc(name)}`, { method: 'DELETE', headers: headers() });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(typeof body.detail === 'string' ? body.detail : `the server answered ${res.status}`);
      }
      await loadDeals();
    } catch (err) {
      button.disabled = false;
      window.alert(`"${name}" could not be removed: ${err.message}`);
    }
  }

  function card(d) {
    const saved = d.reviewed_at ? new Date(d.reviewed_at) : null;
    const link = el('a', { class: 'deal-card', href: dealUrl(d.name) },
      el('span', { class: `deal-state ${saved ? 'is-saved' : 'is-new'}`, text: saved ? `Reviewed ${saved.toLocaleDateString()}` : 'Not reviewed yet' }),
      el('span', { class: 'deal-name', text: d.name }),
      el('span', { class: 'deal-meta', text: `${plural(d.documents, 'document', 'documents')}${d.removable ? ' · added by you' : ''}` }),
      el('span', { class: 'deal-open' }, document.createTextNode(saved ? 'Continue review' : 'Open and check'), el('span', { 'aria-hidden': 'true', text: ' →' })));
    if (!d.removable) return el('li', null, link);
    const remove = el('button', { type: 'button', class: 'deal-remove', 'aria-label': `Remove ${d.name}`, text: 'Remove' });
    remove.addEventListener('click', () => removeDeal(d.name, remove));
    return el('li', { class: 'is-own' }, link, remove);
  }

  /* ---- a new deal from the visitor's own folder or files ---- */

  const SUPPORTED = /\.(pdf|docx|txt|md|markdown|csv|tsv|json|log|html?|xml)$/i;
  const MAX_FILES = 50;
  const MAX_BYTES = 15 * 1024 * 1024;

  // A folder or file name the server accepts: no separators, not hidden, not too long.
  const cleanName = (s) => String(s || '').replace(/[\\/:*?"<>|]+/g, '-').replace(/^[._\s]+/, '').trim().slice(0, 120);

  async function addDeal(fileList, fromFolder) {
    const status = $('new-deal-status');
    const files = Array.from(fileList || []);
    if (!files.length) return;
    const top = fromFolder && files[0].webkitRelativePath ? files[0].webkitRelativePath.split('/')[0] : '';
    const name = cleanName($('new-deal-name').value) || cleanName(top) || `My deal ${new Date().toISOString().slice(0, 10)}`;
    const items = [];
    const skipped = [];
    for (const f of files) {
      const parts = (fromFolder && f.webkitRelativePath ? f.webkitRelativePath.split('/').slice(1) : [f.name]).map(cleanName);
      if (!SUPPORTED.test(f.name) || parts.some((p) => !p)) { skipped.push(f.name); continue; }
      if (f.size > MAX_BYTES) { skipped.push(`${f.name} (over 15 MB)`); continue; }
      items.push({ file: f, path: parts.join('/') });
    }
    if (!items.length) { status.textContent = 'None of those files can be checked: choose PDF, Word, text, CSV or HTML files.'; return; }
    if (items.length > MAX_FILES) { status.textContent = `That is ${items.length} documents; a deal can hold ${MAX_FILES}. Choose a smaller folder.`; return; }
    const failed = [];
    for (let i = 0; i < items.length; i += 1) {
      status.textContent = `Adding ${i + 1} of ${items.length} to "${name}": ${items[i].path}`;
      try {
        const res = await fetch(`/api/v1/deals/${enc(name)}/files/${items[i].path.split('/').map(enc).join('/')}`, {
          method: 'PUT', headers: headers({ 'Content-Type': 'application/octet-stream' }), body: items[i].file,
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          failed.push(`${items[i].path}: ${typeof body.detail === 'string' ? body.detail : res.status}`);
        }
      } catch (err) {
        failed.push(`${items[i].path}: the server could not be reached`);
      }
    }
    if (failed.length === items.length) { status.textContent = `Nothing could be added. ${failed[0]}`; return; }
    status.textContent = `Added ${plural(items.length - failed.length, 'document', 'documents')}${skipped.length ? `; skipped ${skipped.length} that cannot be checked` : ''}. Opening "${name}"...`;
    window.location.href = dealUrl(name);
  }

  /* ---- the visitor's demo code ---- */

  function showCode(message) {
    $('demo-code-value').textContent = window.FinePrintVisitor.key();
    $('demo-code-status').textContent = message || '';
  }

  function switchCode(code, message) {
    if (!window.FinePrintVisitor.set(code)) {
      $('demo-code-status').textContent = 'A demo code is at least 12 letters or numbers (dashes are fine).';
      return;
    }
    $('demo-code-form').hidden = true;
    $('demo-code-change').setAttribute('aria-expanded', 'false');
    showCode(message);
    loadDeals();
  }

  function wireDemoCode() {
    if (!window.FinePrintVisitor) return;
    showCode();
    $('demo-code-copy').addEventListener('click', () => {
      navigator.clipboard.writeText(window.FinePrintVisitor.key())
        .then(() => showCode('Copied. Keep it to come back to your work.'))
        .catch(() => showCode('Select the code and copy it yourself.'));
    });
    $('demo-code-change').addEventListener('click', () => {
      const form = $('demo-code-form');
      form.hidden = !form.hidden;
      $('demo-code-change').setAttribute('aria-expanded', String(!form.hidden));
      if (!form.hidden) $('demo-code-input').focus();
    });
    $('demo-code-form').addEventListener('submit', (e) => {
      e.preventDefault();
      switchCode($('demo-code-input').value, 'Switched. These are the deals and reviews kept under this code.');
    });
    $('demo-code-new').addEventListener('click', () => {
      switchCode(window.FinePrintVisitor.fresh(), 'A new code: you start with a clean review of every deal.');
    });
  }

  function wireNewDeal() {
    $('pick-folder-btn').addEventListener('click', () => $('pick-folder').click());
    $('pick-files-btn').addEventListener('click', () => $('pick-files').click());
    $('pick-folder').addEventListener('change', (e) => addDeal(e.target.files, true));
    $('pick-files').addEventListener('change', (e) => addDeal(e.target.files, false));
  }

  async function loadDeals() {
    try {
      const res = await fetch('/api/v1/deals', { headers: headers() });
      if (res.status === 401) {
        const rejected = Boolean(savedCode());
        forgetCode();   // a code from an earlier visit is not sent again, so the box starts empty
        askForCode(rejected);
        return;
      }
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `the server answered ${res.status}`);
      state.deals = body.deals;
      state.root = body.root;
      $('deals-root').textContent = body.root;
      $('deal-list').replaceChildren(...body.deals.map(card));
      $('deals-empty').hidden = body.deals.length > 0;
      $('deals-error').hidden = true;
      $('new-deal').hidden = !body.uploads;
    } catch (err) {
      $('deals-root').textContent = 'not available';
      $('deals-error').hidden = false;
      $('deals-error').textContent = `The deal folders could not be listed: ${err.message}`;
    }
  }

  async function loadStatus() {
    try {
      const res = await fetch('/api/v1/status', { headers: { Accept: 'application/json' } });
      state.live = res.ok && Boolean((await res.json()).live);
    } catch (err) {
      state.live = false;
    }
  }

  async function askModel(text) {
    if (!state.live) return null;
    const res = await fetch('/api/v1/assistant/interpret', {
      method: 'POST', headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ text: text.slice(0, 500), deals: state.deals.map((d) => d.name) }),
    });
    return res.ok ? res.json() : null;
  }

  async function handle(text) {
    let c = AS ? AS.parseCommand(text, { deals: state.deals.map((d) => d.name) }) : { action: 'unknown' };
    if (c.action === 'unknown') {
      const asked = await askModel(text).catch(() => null);
      c = asked && asked.action !== 'unknown' ? asked : c.guess || c;   // no AI answer: fall back to the phrase match
    }
    const names = state.deals.map((d) => d.name);
    switch (c.action) {
      case 'stop': return '';
      case 'list-deals': {
        await loadDeals();
        const listed = state.deals.map((d) => d.name);
        return listed.length ? `You have ${plural(listed.length, 'deal folder', 'deal folders')}: ${listed.join(', ')}.` : `There are no deal folders yet. Make one folder per deal in ${state.root}.`;
      }
      case 'open-deal':
        window.location.href = dealUrl(c.deal);
        return `Opening ${c.deal}…`;
      case 'problems':
      case 'summary':
        if (c.deal) {
          window.location.href = dealUrl(c.deal, text);
          return `Opening ${c.deal}…`;
        }
        return names.length ? `Which deal? Say "open" and one of: ${names.join(', ')}.` : 'There are no deal folders yet.';
      case 'help':
        return 'Say "open" and a deal name, or "what\'s wrong with" a deal. Once a deal is open you can say "next problem", "not a problem", "show tax problems" or "export".';
      default:
        return `Sorry, I did not understand "${text}". Say "open" and a deal name${names.length ? ` (${names.join(', ')})` : ''}.`;
    }
  }

  adoptCodeFromLink();
  wireDemoCode();
  wireNewDeal();
  loadDeals();
  loadStatus();
  if (window.FinePrintVoice) {
    window.FinePrintVoice.mount({
      handle,
      greeting: 'Which deal shall we look at? Say "open" and its name, or ask "what\'s wrong with" a deal.',
    });
  }
})();
