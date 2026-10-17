// PARSEC dashboard. Renders exactly what POST /api/v1/data-room/run returns. There is no sample data in this file.
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const MAX_FILE_BYTES = 5 * 1024 * 1024;
  const SEVERITY_LABEL = { deal_breaker: 'Deal breaker', price_adjuster: 'Price adjuster', manageable: 'Manageable', minor: 'Minor' };
  const STATUS_LABEL = { broken: 'Broken', holding: 'Holding', at_risk: 'At risk', not_tested: 'Not tested' };
  const UNAVAILABLE_TEXT = {
    llm: 'Committee questions need the Nebius model, which is not configured or did not answer.',
    web_check: 'The public-source check did not run.',
  };

  const state = { docs: [], deal: null, activeFinding: null };

  function h(tag, props, ...kids) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else node.setAttribute(k, v === true ? '' : String(v));
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) node.append(kid);
    return node;
  }

  const money = (n) => `$${Math.round(n).toLocaleString('en-US')}`;
  const millions = (n) => `$${(n / 1e6).toFixed(2)}M`;
  const setStatus = (msg, isError) => { const s = $('status'); s.textContent = msg || ''; s.classList.toggle('is-error', Boolean(isError)); };

  // The server may require an access code for its paid endpoints; it says so on /api/v1/status.
  const CODE_KEY = 'parsec-access-code';
  const accessCode = () => ($('access-code') ? $('access-code').value.trim() : '');

  async function loadAccessRequirement() {
    try {
      const res = await fetch('/api/v1/status');
      if (!res.ok) return;
      const s = await res.json();
      if (!s.auth_required) return;
      $('code-row').hidden = false;
      try { $('access-code').value = sessionStorage.getItem(CODE_KEY) || ''; } catch (err) { /* storage blocked: type it each time */ }
      $('access-code').addEventListener('change', (e) => {
        try { sessionStorage.setItem(CODE_KEY, e.target.value.trim()); } catch (err) { /* not remembered */ }
      });
    } catch (err) { /* opened without the server: nothing to set up */ }
  }

  async function api(path, options) {
    const opts = Object.assign({}, options);
    const code = accessCode();
    if (code) opts.headers = Object.assign({}, opts.headers, { 'X-Review-Token': code });
    const res = await fetch(path, opts);
    let body = null;
    try { body = await res.json(); } catch (err) { /* non-JSON error page */ }
    if (!res.ok) {
      const detail = body && typeof body.detail === 'string' ? body.detail : `Request failed (HTTP ${res.status}).`;
      throw new Error(detail);
    }
    return body;
  }

  /* ---------------------------------------------------------------- input */
  function refreshInput() {
    $('doc-list').replaceChildren(...state.docs.map((d, i) => h('li', null,
      h('span', { text: `${d.name} (${d.text.length.toLocaleString()} characters)` }),
      h('button', { type: 'button', class: 'link', text: 'Remove', 'aria-label': `Remove ${d.name}`, 'data-i': i }))));
    const hasInput = state.docs.length > 0 || $('paste').value.trim().length > 0;
    $('run').disabled = !hasInput;
    $('lookup').disabled = !$('company').value.trim();
  }

  // Documents are sent keyed by name, so two files called "ledger.csv" would overwrite each other.
  // The second becomes "ledger (2).csv", the third "ledger (3).csv"; the extension is kept.
  function uniqueName(name, taken) {
    if (!taken.has(name)) return name;
    const dot = name.lastIndexOf('.');
    const stem = dot > 0 ? name.slice(0, dot) : name;
    const ext = dot > 0 ? name.slice(dot) : '';
    let n = 2;
    while (taken.has(`${stem} (${n})${ext}`)) n += 1;
    return `${stem} (${n})${ext}`;
  }

  async function addFiles(fileList) {
    for (const file of Array.from(fileList)) {
      if (file.size > MAX_FILE_BYTES) { setStatus(`${file.name} is larger than 5 MB.`, true); continue; }
      const text = await file.text();
      if (!text.trim()) { setStatus(`${file.name} has no text.`, true); continue; }
      const name = uniqueName(file.name, new Set(state.docs.map((d) => d.name)));
      state.docs = [...state.docs, { name, text }];
    }
    $('files').value = '';
    refreshInput();
  }

  function collectDocuments() {
    const documents = {};
    const taken = new Set();
    const add = (name, text) => {
      const unique = uniqueName(name, taken);
      taken.add(unique);
      documents[unique] = text;
    };
    state.docs.forEach((d) => add(d.name, d.text));
    const pasted = $('paste').value.trim();
    if (pasted) add('Pasted_document.txt', pasted);
    return documents;
  }

  const isWebUrl = (url) => /^https?:\/\//i.test(String(url || ''));

  /* ---------------------------------------------------------------- render */
  function renderMeters(deal) {
    const t = deal.tokens_meter || {};
    const w = deal.tavily_meter || {};
    const part = (label, value) => h('span', { class: 'meter-badge' }, `${label}: `, h('strong', { text: value }));
    $('meters').replaceChildren(
      part('Nano tokens', Number(t.nano || 0).toLocaleString()),
      part('Super tokens', Number(t.super || 0).toLocaleString()),
      part('Ultra tokens', Number(t.ultra || 0).toLocaleString()),
      part('Web searches', w.available ? String(w.api_calls || 0) : 'unavailable'),
      part('Model cost', `$${Number(t.total_cost_usd || 0).toFixed(4)}`),
    );
  }

  // The company's own Adjusted EBITDA reconciliation, read by the model and checked line by line and by arithmetic.
  const STANDARD_LINE = /interest|tax|depreciation|amortization|amortisation/i;
  const readRec = (deal) => {
    const r = deal.ebitda_reconciliation;
    return r && (r.status === 'foots' || r.status === 'does_not_foot') ? r : null;
  };
  const num = (v) => Number(v).toLocaleString('en-US', { maximumFractionDigits: 2 });

  function renderHero(deal) {
    const box = $('not-computed');
    const stats = $('hero-stats');
    const rec = readRec(deal);
    if (!deal.hero_metric && rec) {
      const units = rec.units || 'units as stated';
      box.textContent = rec.status === 'foots'
        ? `Adjusted EBITDA of ${num(rec.total.value)} (${units}) for ${rec.period || 'the latest period'}, read from ${rec.document}. Every line was found in the document and the lines add up to it. See the add-backs to evidence below.`
        : `The reconciliation in ${rec.document} was read but its lines give ${num(rec.computed_total)} against a stated ${num(rec.total.value)} (${units}). Check it by hand before relying on it.`;
      box.hidden = false;
      stats.hidden = true;
      return;
    }
    if (!deal.hero_metric) {
      const missing = deal.findings.find((f) => f.id === 'FINDING-BASE-EBITDA-MISSING');
      box.textContent = missing ? missing.description : 'Adjusted EBITDA was not computed.';
      box.hidden = false;
      stats.hidden = true;
      return;
    }
    box.hidden = true;
    stats.hidden = false;
    const m = deal.hero_metric;
    $('val-claimed').textContent = money(m.claimed_ebitda);
    $('val-adjudicated').textContent = money(m.parsec_ebitda);
    $('val-delta').textContent = m.delta_ebitda ? `-${money(m.delta_ebitda)}` : 'None';
    $('note-delta').textContent = m.delta_ebitda ? 'Claimed add-backs the ledger contradicts' : 'No add-back was contradicted';
    $('val-overpayment').textContent = m.potential_overpayment_avoided ? millions(m.potential_overpayment_avoided) : 'None';
    $('note-multiple').textContent = `At ${m.multiple}x EBITDA`;
  }

  function renderFindings(deal) {
    const list = $('findings-container');
    if (!deal.findings.length) {
      list.replaceChildren(h('p', { class: 'muted', text: 'No findings. That is not a clean bill of health: it means none of the checks this pipeline runs fired on these documents.' }));
      return;
    }
    list.replaceChildren(...deal.findings.map((f) => {
      const quote = f.evidence && f.evidence[0] ? f.evidence[0].exact_text_quote : '';
      return h('button', { type: 'button', class: `finding-card severity-${f.severity}`, 'data-id': f.id },
        h('span', { class: 'finding-header' },
          h('span', { class: 'finding-title', text: f.title }),
          h('span', { class: `badge ${f.severity === 'deal_breaker' ? 'badge-deal-breaker' : 'badge-price-adjuster'}`, text: SEVERITY_LABEL[f.severity] || f.severity })),
        h('span', { class: 'finding-desc', text: f.description }),
        f.price_impact_usd > 0 ? h('span', { class: 'finding-impact', text: `Price impact: -${millions(f.price_impact_usd)}` }) : null,
        quote ? h('span', { class: 'finding-quote', text: `"${quote.length > 110 ? `${quote.slice(0, 110)}...` : quote}"` })
              : h('span', { class: 'finding-quote muted', text: 'No source text was located for this finding.' }));
    }));
  }

  function renderBridge(deal) {
    const body = $('bridge-tbody');
    const steps = deal.bridge ? deal.bridge.waterfall_steps : [];
    const rec = readRec(deal);
    $('bridge-source').textContent = !steps.length && rec
      ? 'Lines read by Nemotron, each traced to the document; sums checked in code'
      : 'Computed in code, not by a model';
    if (!steps.length && rec) {
      let running = Number(rec.start.value);
      const rows = [h('tr', null,
        h('td', { text: rec.start.label }), h('td', { class: 'num', text: num(rec.start.value) }),
        h('td', { class: 'center' }, h('span', { class: 'muted', text: 'Starting line' })), h('td', { class: 'num', text: num(running) }))];
      rec.adjustments.forEach((a) => {
        running += Number(a.value);
        const addback = Number(a.value) > 0 && !STANDARD_LINE.test(a.label);
        rows.push(h('tr', null,
          h('td', { text: a.label }), h('td', { class: 'num', text: num(a.value) }),
          h('td', { class: 'center' }, Number(a.value) === 0 ? h('span', { class: 'muted', text: 'None this period' })
            : h('span', { class: `badge ${addback ? 'badge-price-adjuster' : 'badge-verified'}`, text: addback ? 'Add-back to evidence' : 'Standard' })),
          h('td', { class: 'num', text: num(running) })));
      });
      rows.push(h('tr', null,
        h('td', { text: `${rec.total.label} (stated)` }), h('td', { class: 'num', text: num(rec.total.value) }),
        h('td', { class: 'center' }, h('span', { class: `badge ${rec.status === 'foots' ? 'badge-verified' : 'badge-deal-breaker'}`, text: rec.status === 'foots' ? 'Adds up' : 'Does not add up' })),
        h('td', { class: 'num', text: num(rec.computed_total) })));
      body.replaceChildren(...rows);
      return;
    }
    if (!steps.length) {
      body.replaceChildren(h('tr', null, h('td', { colspan: 4, class: 'muted', text: 'No bridge: base EBITDA was not found.' })));
      return;
    }
    body.replaceChildren(...steps.map((s, i) => h('tr', null,
      h('td', { text: s.step }),
      h('td', { class: 'num', text: money(Math.abs(s.amount)) }),
      h('td', { class: 'center' }, i === 0 ? h('span', { class: 'muted', text: 'Reported' })
        : h('span', { class: `badge ${s.accepted ? 'badge-verified' : 'badge-deal-breaker'}`, text: s.accepted ? 'Accepted' : 'Disallowed' })),
      h('td', { class: 'num', text: money(s.cumulative) }))));
  }

  function renderKillCriteria(deal) {
    const box = $('kill-criteria-container');
    if (!deal.kill_criteria.length) {
      box.replaceChildren(h('p', { class: 'muted', text: 'No playbook for this sector.' }));
      return;
    }
    box.replaceChildren(...deal.kill_criteria.map((kc) => h('div', { class: `kc kc-${kc.status}` },
      h('div', { class: 'kc-head' },
        h('strong', { text: kc.title }),
        h('span', { class: `badge ${kc.status === 'broken' ? 'badge-deal-breaker' : (kc.status === 'holding' ? 'badge-verified' : 'badge-price-adjuster')}`, text: STATUS_LABEL[kc.status] || kc.status })),
      h('p', { class: 'muted', text: kc.condition }))));
  }

  function renderCommittee(deal) {
    const questions = (deal.ic_grilling && deal.ic_grilling.questions) || [];
    $('ic-note').textContent = questions.length ? 'Written by the model from the findings above' : '';
    $('ic-container').replaceChildren(...(questions.length
      ? questions.map((q) => h('li', { text: String(q) }))
      : [h('li', { class: 'muted', text: 'No questions generated.' })]));
  }

  function renderNotices(deal) {
    const box = $('unavailable');
    const reasons = [];
    (deal.unavailable || []).forEach((k) => {
      let text = UNAVAILABLE_TEXT[k] || `${k} was unavailable.`;
      if (k === 'web_check' && deal.tavily_meter && deal.tavily_meter.reason) text += ` ${deal.tavily_meter.reason}`;
      reasons.push(text);
    });
    box.textContent = reasons.join(' ');
    box.hidden = reasons.length === 0;
    $('gaps').replaceChildren(...(deal.data_room_gaps.length
      ? deal.data_room_gaps.map((g) => h('li', { text: g }))
      : [h('li', { class: 'muted', text: 'Every expected item for this playbook was supplied.' })]));
  }

  function renderDeal(deal) {
    state.deal = deal;
    $('results').hidden = false;
    $('deal-title').textContent = deal.company_name;
    $('deal-subtitle').textContent = `${deal.deal_id}. ${deal.findings.length} finding${deal.findings.length === 1 ? '' : 's'}.`;
    renderMeters(deal);
    renderHero(deal);
    renderNotices(deal);
    renderFindings(deal);
    renderBridge(deal);
    renderKillCriteria(deal);
    renderCommittee(deal);
    $('results').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function renderLookup(data) {
    state.deal = null;
    $('results').hidden = false;
    $('deal-title').textContent = data.company_name;
    $('deal-subtitle').textContent = 'Public-source check only. No documents were analysed.';
    $('hero-stats').hidden = true;
    const box = $('not-computed');
    box.hidden = false;
    box.textContent = data.available
      ? `${data.risk_flags.length} public source${data.risk_flags.length === 1 ? '' : 's'} mention a dispute. Confirm each concerns this company.`
      : `Unavailable: ${data.reason}`;
    $('unavailable').hidden = true;
    $('meters').replaceChildren();
    $('findings-container').replaceChildren(...(data.sources.length
      ? data.sources.map((s) => h('div', { class: 'finding-card' },
        h('span', { class: 'finding-title' }, isWebUrl(s.url)
          ? h('a', { href: s.url, target: '_blank', rel: 'noopener noreferrer', text: s.title || s.url })
          : h('span', { text: s.title || String(s.url || '') })),
        h('span', { class: 'finding-desc', text: s.snippet })))
      : [h('p', { class: 'muted', text: 'No results named this company.' })]));
    $('bridge-tbody').replaceChildren();
    $('kill-criteria-container').replaceChildren();
    $('ic-container').replaceChildren();
    $('gaps').replaceChildren();
  }

  /* ---------------------------------------------------------------- evidence modal */
  function openEvidence(finding) {
    state.activeFinding = finding;
    $('modal-title').textContent = finding.title;
    $('modal-description').textContent = finding.description;
    $('modal-status').textContent = finding.signed_by_analyst ? 'Already marked as reviewed.' : '';
    const evidence = finding.evidence || [];
    $('modal-evidence').replaceChildren(...(evidence.length ? evidence.map((ev) => {
      const where = ev.char_start != null ? `characters ${ev.char_start} to ${ev.char_end}` : 'position not recorded';
      const isUrl = isWebUrl(ev.document_id);
      return h('div', { class: 'evidence' },
        h('div', { class: 'muted' }, isUrl
          ? h('a', { href: ev.document_id, target: '_blank', rel: 'noopener noreferrer', text: ev.document_name || ev.document_id })
          : `${ev.document_name}, ${where}`),
        h('pre', { class: 'quote', text: ev.exact_text_quote }),
        h('div', { class: 'muted hash', text: `SHA-256 of source: ${ev.content_hash}` }));
    }) : [h('p', { class: 'muted', text: 'No source text was located for this finding.' })]));
    $('evidence-modal').hidden = false;
    $('modal-close').focus();
  }

  async function sign() {
    if (!state.deal || !state.activeFinding) return;
    try {
      await api(`/api/v1/data-room/${encodeURIComponent(state.deal.deal_id)}/findings/${encodeURIComponent(state.activeFinding.id)}/sign`, { method: 'POST' });
      state.activeFinding.signed_by_analyst = true;
      $('modal-status').textContent = 'Marked as reviewed.';
    } catch (err) {
      $('modal-status').textContent = err.message;
    }
  }

  /* ---------------------------------------------------------------- wiring */
  async function run() {
    const documents = collectDocuments();
    $('run').disabled = true;
    setStatus('Analysing...');
    try {
      const deal = await api('/api/v1/data-room/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          company_name: $('company').value.trim() || 'Unnamed target',
          multiple: Number($('multiple').value) || 10,
          sector: $('sector').value,
          documents,
        }),
      });
      setStatus('');
      renderDeal(deal);
    } catch (err) {
      setStatus(err.message, true);
    } finally {
      refreshInput();
    }
  }

  async function lookup() {
    const name = $('company').value.trim();
    if (!name) return;
    setStatus('Searching public sources...');
    try {
      renderLookup(await api(`/api/v1/companies/lookup?company_name=${encodeURIComponent(name)}`));
      setStatus('');
    } catch (err) {
      setStatus(err.message, true);
    }
  }

  $('files').addEventListener('change', (e) => addFiles(e.target.files));
  $('paste').addEventListener('input', refreshInput);
  $('company').addEventListener('input', refreshInput);
  $('doc-list').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-i]');
    if (!btn) return;
    state.docs.splice(Number(btn.dataset.i), 1);
    refreshInput();
  });
  $('run').addEventListener('click', run);
  $('lookup').addEventListener('click', lookup);
  $('findings-container').addEventListener('click', (e) => {
    const card = e.target.closest('[data-id]');
    if (!card || !state.deal) return;
    const finding = state.deal.findings.find((f) => f.id === card.dataset.id);
    if (finding) openEvidence(finding);
  });
  $('modal-close').addEventListener('click', () => { $('evidence-modal').hidden = true; });
  $('evidence-modal').addEventListener('click', (e) => { if (e.target.id === 'evidence-modal') $('evidence-modal').hidden = true; });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') $('evidence-modal').hidden = true; });
  $('modal-sign-btn').addEventListener('click', sign);

  refreshInput();
  loadAccessRequirement();
})();
