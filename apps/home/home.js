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
  // The access code, when this server needs one, as typed on the Check files page (kept for this browser tab).
  function headers(extra) {
    const out = Object.assign({ Accept: 'application/json' }, extra || {});
    let code = '';
    try { code = sessionStorage.getItem('fineprint-access-code') || ''; } catch (err) { /* not available */ }
    if (code) out['X-Review-Token'] = code;
    return out;
  }
  const dealUrl = (name, ask) => `/review/?deal=${enc(name)}${ask ? `&ask=${enc(ask)}` : ''}`;

  function card(d) {
    const saved = d.reviewed_at ? new Date(d.reviewed_at) : null;
    return el('li', null, el('a', { class: 'deal-card', href: dealUrl(d.name) },
      el('span', { class: `deal-state ${saved ? 'is-saved' : 'is-new'}`, text: saved ? `Reviewed ${saved.toLocaleDateString()}` : 'Not reviewed yet' }),
      el('span', { class: 'deal-name', text: d.name }),
      el('span', { class: 'deal-meta', text: plural(d.documents, 'document', 'documents') }),
      el('span', { class: 'deal-open' }, document.createTextNode(saved ? 'Continue review' : 'Open and check'), el('span', { 'aria-hidden': 'true', text: ' →' }))));
  }

  async function loadDeals() {
    try {
      const res = await fetch('/api/v1/deals', { headers: headers() });
      if (res.status === 401) throw new Error('this server needs an access code. Enter it once on the Check files page, then come back');
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `the server answered ${res.status}`);
      state.deals = body.deals;
      state.root = body.root;
      $('deals-root').textContent = body.root;
      $('deal-list').replaceChildren(...body.deals.map(card));
      $('deals-empty').hidden = body.deals.length > 0;
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
      case 'list-deals':
        await loadDeals();
        return names.length ? `You have ${plural(names.length, 'deal folder', 'deal folders')}: ${names.join(', ')}.` : `There are no deal folders yet. Make one folder per deal in ${state.root}.`;
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

  loadDeals();
  loadStatus();
  if (window.FinePrintVoice) {
    window.FinePrintVoice.mount({
      handle,
      greeting: 'Which deal shall we look at? Say "open" and its name, or ask "what\'s wrong with" a deal.',
    });
  }
})();
