/* Fine Print UI. Builds every node with createElement/textContent so document text is never parsed as HTML. */
(function () {
  'use strict';

  const FP = window.FinePrint;
  const DS = window.FinePrintDossier;
  const WF = window.FinePrintWorkflow;
  const $ = (id) => document.getElementById(id);

  const MAX_BYTES = 15 * 1024 * 1024;
  const MAX_CHARS = 1500000;
  const MAX_DOCS = 25;
  const SEV_LABEL = { critical: 'Critical', high: 'High', medium: 'Medium', low: 'Low' };
  const PDFJS_URL = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js';
  const PDFJS_WORKER = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
  const MAMMOTH_URL = 'https://cdnjs.cloudflare.com/ajax/libs/mammoth/1.6.0/mammoth.browser.min.js';
  const TEXT_EXT = ['txt', 'md', 'markdown', 'csv', 'tsv', 'json', 'log', 'html', 'htm', 'xml'];

  const narrow = window.matchMedia('(max-width: 959px)');
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  const state = {
    docs: [],
    analyzed: [],
    lenses: new Set(FP.LENSES.map((l) => l.id)),
    result: null,
    dismissed: new Set(),
    confirmed: new Set(),
    treatments: new Map(), // finding id -> {key, text}: the reviewer's treatment, replacing the suggestion
    requestStatus: new Map(), // request key -> {status, note}: the reviewer's status on a request
    reviewer: '',
    severity: 'all',
    lens: 'all',
    showDismissed: false,
    activeDoc: 0,
    selected: null,
    cursor: {},
    view: 'findings',
    seq: 0,
    ai: { available: false, live: false, model: '' },
    aiSummary: null,
  };

  const PRIVACY_LOCAL = 'Documents are checked in this browser. Nothing is uploaded.';
  const PRIVACY_AI = 'Rule-based checks run in this browser. The options you ticked send document text to this server.';
  const AI_MAX_DOCS = 10;
  const AI_MAX_CHARS = 200000;

  /* ---------------------------------------------------------------- */
  /* small DOM helper                                                  */
  /* ---------------------------------------------------------------- */

  function h(tag, props, ...kids) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v === false || v == null) continue;
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? '' : String(v));
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) node.append(kid);
    return node;
  }

  function plural(n, one, many) {
    return `${n.toLocaleString()} ${n === 1 ? one : many}`;
  }

  function setStatus(message, isError) {
    const s = $('status');
    s.textContent = message || '';
    s.classList.toggle('is-error', Boolean(isError));
  }

  let toastTimer = null;
  function toast(message) {
    let t = document.querySelector('.toast');
    if (!t) {
      t = h('div', { class: 'toast', role: 'status' });
      document.body.append(t);
    }
    t.textContent = message;
    t.classList.add('is-visible');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('is-visible'), 2600);
  }

  /* ---------------------------------------------------------------- */
  /* setup: lenses, documents, samples                                 */
  /* ---------------------------------------------------------------- */

  function renderLenses() {
    const list = $('lens-list');
    list.replaceChildren(
      h('li', null,
        h('label', { class: 'lens is-fixed' },
          h('input', { type: 'checkbox', checked: true, disabled: true }),
          h('span', { class: 'lens-name', text: FP.INTEGRITY.name }),
          h('span', { class: 'lens-blurb', text: FP.INTEGRITY.blurb }))),
      ...FP.LENSES.map((lens) => h('li', null,
        h('label', { class: 'lens' },
          h('input', {
            type: 'checkbox', value: lens.id, checked: state.lenses.has(lens.id),
            onchange: (e) => {
              if (e.target.checked) state.lenses.add(lens.id);
              else state.lenses.delete(lens.id);
              updateRunState();
            },
          }),
          h('span', { class: 'lens-name', text: lens.name }),
          h('span', { class: 'lens-blurb', text: lens.blurb }))))
    );
  }

  function renderDocs() {
    $('doc-list').replaceChildren(...state.docs.map((d) => h('li', { class: 'doc-item' },
      h('div', null,
        h('div', { class: 'doc-name', text: d.name }),
        h('div', { class: 'doc-meta', text: plural(d.words, 'word', 'words') })),
      h('button', {
        type: 'button', class: 'link doc-remove', 'aria-label': `Remove ${d.name}`, text: 'Remove',
        onclick: () => {
          state.docs = state.docs.filter((x) => x.id !== d.id);
          renderDocs();
          updateRunState();
        },
      }))));
  }

  // Real public documents a first-time visitor can try. Offered only when the page is served (not opened from disk).
  async function loadExamples() {
    let manifest;
    try {
      const res = await fetch('examples/manifest.json');
      if (!res.ok) return;
      manifest = await res.json();
    } catch (err) {
      return;
    }
    const sets = new Map(manifest.sets.map((x) => [x.id, x]));
    const filesOf = (set) => (set.files_from ? set.files_from.flatMap((id) => filesOf(sets.get(id))) : set.files);
    const buttons = manifest.sets.map((set) => h('button', {
      type: 'button', class: 'secondary', text: set.label,
      onclick: async () => {
        const files = filesOf(set);
        try {
          for (const f of files) {
            const res = await fetch(`examples/${f.name}`);
            if (!res.ok) throw new Error(`could not load ${f.name}`);
            addDocument(f.title, await res.text());
          }
          $('example-source').textContent = files.map((f) => `${f.title}. Source: ${f.source}, ${f.licence}.`).join(' ');
        } catch (err) {
          setStatus(`The example could not be loaded: ${err.message}`, true);
        }
      },
    }));
    $('example-buttons').replaceChildren(...buttons);
    $('examples').hidden = false;
  }

  function updateRunState() {
    const pending = $('paste').value.trim().length > 0;
    const hasInput = state.docs.length > 0 || pending;
    $('run').disabled = !hasInput;
    const lensCount = state.lenses.size;
    let hint = 'Add at least one document to begin.';
    if (hasInput) {
      const n = state.docs.length + (pending ? 1 : 0);
      hint = `${plural(n, 'document', 'documents')} against ${lensCount === 0 ? 'integrity checks only' : plural(lensCount, 'area', 'areas')}.`;
    }
    $('run-hint').textContent = hint;
  }

  function addDocument(name, text) {
    const clean = String(text).replace(/\r\n?/g, '\n').replace(/\u0000/g, '');
    if (!clean.trim()) throw new Error(`${name} has no readable text.`);
    if (clean.length > MAX_CHARS) {
      throw new Error(`${name} is very large (${clean.length.toLocaleString()} characters). Split it into parts under ${MAX_CHARS.toLocaleString()} characters.`);
    }
    if (state.docs.length >= MAX_DOCS) throw new Error(`You can check up to ${MAX_DOCS} documents at a time.`);
    state.docs = [...state.docs, { id: ++state.seq, name, text: clean, words: FP.wordCount(clean) }];
    renderDocs();
    updateRunState();
  }

  function addPasted() {
    const text = $('paste').value;
    if (!text.trim()) {
      setStatus('Paste some text first.', true);
      return false;
    }
    const given = $('paste-name').value.trim();
    const count = state.docs.filter((d) => d.name.startsWith('Pasted text')).length;
    try {
      addDocument(given || (count ? `Pasted text ${count + 1}` : 'Pasted text'), text);
    } catch (err) {
      setStatus(err.message, true);
      return false;
    }
    $('paste').value = '';
    $('paste-name').value = '';
    updateRunState();
    setStatus('Added.');
    return true;
  }

  const scriptCache = {};
  function loadScript(url) {
    if (!scriptCache[url]) {
      scriptCache[url] = new Promise((resolve, reject) => {
        const s = document.createElement('script');
        s.src = url;
        s.onload = resolve;
        s.onerror = () => {
          delete scriptCache[url];
          reject(new Error('The PDF or Word reader could not be loaded. Check your connection, or paste the text instead.'));
        };
        document.head.append(s);
      });
    }
    return scriptCache[url];
  }

  async function readPdf(file) {
    await loadScript(PDFJS_URL);
    const lib = window.pdfjsLib;
    lib.GlobalWorkerOptions.workerSrc = PDFJS_WORKER;
    const pdf = await lib.getDocument({ data: await file.arrayBuffer() }).promise;
    const pages = [];
    for (let p = 1; p <= pdf.numPages; p += 1) {
      const content = await (await pdf.getPage(p)).getTextContent();
      let out = '';
      let lastY = null;
      for (const item of content.items) {
        const y = item.transform ? item.transform[5] : null;
        if (lastY !== null && y !== null && Math.abs(y - lastY) > 2) out += '\n';
        else if (out && !out.endsWith(' ') && !out.endsWith('\n')) out += ' ';
        out += item.str;
        lastY = y;
      }
      pages.push(out.trim());
    }
    const text = pages.join('\n\n');
    if (!text.trim()) throw new Error(`${file.name} has no selectable text. It may be a scan. Run it through OCR first.`);
    return text;
  }

  async function readDocx(file) {
    await loadScript(MAMMOTH_URL);
    const res = await window.mammoth.extractRawText({ arrayBuffer: await file.arrayBuffer() });
    return res.value;
  }

  function htmlToText(source) {
    const spaced = source.replace(/<\/(p|div|li|tr|h[1-6]|table|section|article)>|<br\s*\/?>/gi, '$&\n');
    const doc = new DOMParser().parseFromString(spaced, 'text/html');
    doc.querySelectorAll('script,style,noscript').forEach((n) => n.remove());
    return (doc.body ? doc.body.textContent : spaced).replace(/\n{3,}/g, '\n\n');
  }

  async function readFile(file) {
    const ext = (file.name.split('.').pop() || '').toLowerCase();
    if (file.size > MAX_BYTES) throw new Error(`${file.name} is larger than 15 MB.`);
    if (ext === 'pdf') return readPdf(file);
    if (ext === 'docx') return readDocx(file);
    if (['xlsx', 'xls', 'pptx', 'ppt', 'doc'].includes(ext)) {
      throw new Error(`${file.name}: .${ext} files are not supported. Save it as CSV, PDF or .docx and add that.`);
    }
    if (!TEXT_EXT.includes(ext) && !file.type.startsWith('text/')) {
      throw new Error(`${file.name}: this file type is not supported.`);
    }
    const text = await file.text();
    return ['html', 'htm', 'xml'].includes(ext) ? htmlToText(text) : text;
  }

  async function handleFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    const problems = [];
    let added = 0;
    for (let i = 0; i < files.length; i += 1) {
      setStatus(`Reading ${i + 1} of ${files.length}: ${files[i].name}`);
      try {
        addDocument(files[i].name, await readFile(files[i]));
        added += 1;
      } catch (err) {
        problems.push(err.message || `${files[i].name} could not be read.`);
      }
    }
    if (problems.length) setStatus(`${added ? `Added ${plural(added, 'file', 'files')}. ` : ''}${problems.join(' ')}`, true);
    else setStatus(`Added ${plural(added, 'file', 'files')}.`);
  }

  /* ---------------------------------------------------------------- */
  /* results                                                           */
  /* ---------------------------------------------------------------- */

  const activeFindings = () => state.result.findings.filter((f) => !state.dismissed.has(f.id));

  function visibleFindings() {
    return state.result.findings.filter((f) => (
      (state.showDismissed || !state.dismissed.has(f.id))
      && (state.severity === 'all' || f.severity === state.severity)
      && (state.lens === 'all' || f.lens === state.lens)
    ));
  }

  function followups() {
    const byAsk = new Map();
    for (const f of activeFindings()) {
      if (!byAsk.has(f.ask)) byAsk.set(f.ask, { ask: f.ask, severity: f.severity, titles: [] });
      byAsk.get(f.ask).titles.push(f.title);
    }
    return [...byAsk.values()];
  }

  function aiCoverage() {
    const s = state.aiSummary;
    if (!s) return 'Rule-based checks only.';
    if (s.failed) return `The optional reads did not run: ${s.failed} These are rule-based results only.`;
    const sentences = [];
    if (s.ai) {
      const parts = [`The AI read added ${plural(s.ai.added, 'finding', 'findings')}`];
      if (s.ai.overlapped) parts.push(`${s.ai.overlapped} overlapped ${s.ai.overlapped === 1 ? 'a point' : 'points'} the rules had already flagged`);
      if (s.ai.dropped) parts.push(`${s.ai.dropped} ${s.ai.dropped === 1 ? 'was' : 'were'} discarded because ${s.ai.dropped === 1 ? 'its' : 'their'} quote was not in the document`);
      const adj = s.ai.adjudication;
      if (adj && adj.rejected) parts.push(`Ultra rejected ${adj.rejected}`);
      sentences.push(`${parts.join(', ')}.`);
    }
    if (s.aiError) sentences.push(`The AI read did not run: ${s.aiError}`);
    if (s.web) {
      if (!s.web.available) sentences.push(`The web check did not run: ${s.web.reason}`);
      else if (!s.web.checked.length) sentences.push('The web check found no company names to look up.');
      else {
        const seen = s.web.lookups.length
          ? s.web.lookups.map((l) => (l.error ? `${l.entity} (lookup failed)` : `${l.entity} (${l.relevant} of ${l.results} results relevant)`)).join(', ')
          : s.web.checked.join(', ');
        sentences.push(`The web check looked up ${seen} and found ${plural(s.web.added, 'matter', 'matters')} to confirm.`);
        if (s.web.reason) sentences.push(s.web.reason);
      }
    }
    if (s.ai && s.ai.clauses) {
      const c = s.ai.clauses;
      sentences.push(`The clause check read ${plural(c.candidates, 'candidate passage', 'candidate passages')} picked from all ${c.passages.toLocaleString()} passages of every document${c.failed_calls ? `; ${plural(c.failed_calls, 'batch', 'batches')} could not be read` : ''}.`);
    }
    if (s.ai && s.ai.truncated) sentences.push(s.ai.clauses ? 'The general AI read covered only the first part of long documents.' : 'Only the first part of long documents was read.');
    if (s.ai && s.ai.failedChunks) {
      sentences.push(`${plural(s.ai.failedChunks, 'part', 'parts')} of the documents could not be read by the model, so the AI read is incomplete.`);
    }
    return sentences.join(' ');
  }

  function renderMeter() {
    const box = $('meter');
    const ai = state.aiSummary && state.aiSummary.ai;
    if (!ai || !ai.tiers) { box.hidden = true; box.replaceChildren(); return; }
    const label = { nano: 'Nano', super: 'Super', ultra: 'Ultra' };
    const role = { nano: 'triage', super: 'review', ultra: 'rulings' };
    const cells = Object.keys(label).map((tier) => {
      const m = ai.tiers[tier];
      return h('li', null,
        h('span', { class: 'meter-tier', text: `${label[tier]} ${role[tier]}` }),
        h('span', { class: 'meter-figure', text: m && m.calls ? `${m.tokens.toLocaleString()} tokens, $${m.cost_usd.toFixed(4)}` : 'not used' }));
    });
    const notes = [];
    if (ai.triage && ai.triage.skipped) notes.push(`Nano skipped ${plural(ai.triage.skipped, 'part', 'parts')} of the documents as having nothing to flag.`);
    if (ai.adjudication && ai.adjudication.checked) {
      notes.push(`Ultra ruled on ${plural(ai.adjudication.checked, 'serious finding', 'serious findings')}: ${ai.adjudication.rejected} rejected, ${ai.adjudication.downgraded} downgraded.`);
    }
    const clauseCost = ai.clauses ? ai.clauses.cost_usd || 0 : 0;
    if (ai.clauses) {
      cells.push(h('li', null,
        h('span', { class: 'meter-tier', text: 'Super clause check' }),
        h('span', { class: 'meter-figure', text: `${plural(ai.clauses.calls, 'call', 'calls')}, $${clauseCost.toFixed(4)}` })));
    }
    box.replaceChildren(
      h('ul', { class: 'meter-tiers', 'aria-label': 'Model usage by tier' }, cells),
      h('p', { class: 'meter-total', text: `Total $${(ai.cost + clauseCost).toFixed(4)}. ${notes.join(' ')}`.trim() }));
    box.hidden = false;
  }

  // The one thing a reviewer must not miss: what is still needed before this file can be relied on.
  function renderNeeds(clearance) {
    const box = $('needs-callout');
    if (!clearance || clearance.state === 'cleared-for-checks') {
      box.hidden = !clearance;
      if (clearance) box.replaceChildren(h('p', { text: clearance.reasons.join(' ') }));
      return;
    }
    const items = [
      ...clearance.missing.map((x) => h('li', { text: x })),
      ...clearance.unsupplied.map((x) => h('li', { text: `${x} (referred to, not supplied)` })),
    ];
    const other = clearance.reasons.filter((r) => !/^Still needed|^Referred to but not supplied/.test(r));
    box.replaceChildren(...[
      h('h3', { text: clearance.state === 'incomplete' ? 'Cannot clear this file yet. Still needed:' : 'Before relying on this, still needed:' }),
      items.length ? h('ul', null, ...items) : null,
      other.length ? h('p', { text: other.join(' ') }) : null,
    ].filter(Boolean));
    box.hidden = false;
  }

  function renderSummary() {
    const active = activeFindings();
    const verdict = FP.verdictFor(active);
    const { stats } = state.result;
    const v = $('verdict');
    const clearance = state.dossier && state.dossier.clearance;
    // "Nothing flagged" is only allowed to read as a pass when the documents and checks were complete.
    const cannotClear = verdict.level === 'none' && clearance && clearance.state === 'incomplete';
    v.textContent = cannotClear ? clearance.headline : verdict.headline;
    v.dataset.level = cannotClear ? 'low' : verdict.level;
    const dismissed = state.dismissed.size;
    $('coverage').textContent = `Checked ${plural(stats.documents, 'document', 'documents')} (${plural(stats.words, 'word', 'words')}) against ${plural(stats.checks, 'check', 'checks')} in ${plural(stats.lenses, 'area', 'areas')}.`
      + `${dismissed ? ` ${plural(dismissed, 'finding is', 'findings are')} dismissed.` : ''}`
      + ` ${aiCoverage()}`
      + ' This is screening: it points at what to ask and does not prove anything.';
    renderNeeds(clearance);
    $('counts').replaceChildren(...FP.SEVERITIES.map((sev) => h('li', { class: `sev-${sev}` },
      h('span', { class: 'swatch', 'aria-hidden': 'true' }),
      h('span', { text: `${verdict.counts[sev]} ${SEV_LABEL[sev].toLowerCase()}` }))));
    $('n-findings').textContent = `(${active.length})`;
    $('n-followups').textContent = `(${followups().length})`;
    renderMeter();
    renderStatus();
  }

  function renderFilters() {
    const base = state.result.findings.filter((f) => state.showDismissed || !state.dismissed.has(f.id));
    const countFor = (sev) => base.filter((f) => (sev === 'all' || f.severity === sev) && (state.lens === 'all' || f.lens === state.lens)).length;
    const chip = (sev, label) => h('button', {
      type: 'button', class: `chip ${sev === 'all' ? '' : `sev-${sev}`}`, 'aria-pressed': String(state.severity === sev),
      onclick: () => { state.severity = sev; renderFilters(); renderFindings(); },
    }, sev === 'all' ? null : h('span', { class: 'swatch', 'aria-hidden': 'true' }), `${label} ${countFor(sev)}`);
    $('severity-chips').replaceChildren(chip('all', 'All'), ...FP.SEVERITIES.map((s) => chip(s, SEV_LABEL[s])));

    const present = [FP.INTEGRITY, ...FP.LENSES].filter((l) => base.some((f) => f.lens === l.id));
    const select = $('lens-filter');
    if (state.lens !== 'all' && !present.some((l) => l.id === state.lens)) state.lens = 'all';
    select.replaceChildren(
      h('option', { value: 'all', text: `All areas (${base.length})` }),
      ...present.map((l) => h('option', { value: l.id, text: `${l.name} (${base.filter((f) => f.lens === l.id).length})` }))
    );
    select.value = state.lens;
  }

  function quoteNode(q) {
    if (!q || !q.text) return null;
    const valid = q.hlStart >= 0 && q.hlEnd > q.hlStart && q.hlEnd <= q.text.length;
    if (!valid) return h('blockquote', { class: 'quote', text: q.text });
    return h('blockquote', { class: 'quote' },
      q.text.slice(0, q.hlStart), h('strong', { text: q.text.slice(q.hlStart, q.hlEnd) }), q.text.slice(q.hlEnd));
  }

  function sourcesNode(f) {
    const safe = (f.sources || []).filter((s) => /^https?:\/\//i.test(s.url || ''));
    if (!safe.length) return null;
    return h('ul', { class: 'sources', 'aria-label': 'Public sources' }, safe.map((s) => h('li', null,
      h('a', { href: s.url, target: '_blank', rel: 'noopener noreferrer', text: s.title || s.url }),
      s.snippet ? h('span', { class: 'snippet', text: s.snippet }) : null)));
  }

  // A screening suggestion from the Paul Hastings framework (see workflow.js), never presented as advice.
  const isSerious = (f) => f.severity === 'critical' || f.severity === 'high';
  const safeId = (id) => String(id).replace(/[^\w-]/g, '_');

  function reviewerLineText(t) {
    return t.reviewer ? `${t.reviewer.remedy} (set by the reviewer; replaces the suggestion)` : '';
  }

  function treatmentSelect(f, t, override) {
    const sid = `tr-${safeId(f.id)}`;
    const options = [
      h('option', { value: '', text: `Keep the suggestion (${t.suggested.remedy})` }),
      ...Object.entries(WF.REMEDY).map(([key, label]) => h('option', { value: key, text: label })),
      h('option', { value: WF.TREATMENT_OTHER, text: 'Other (describe)' }),
    ];
    const select = h('select', { id: sid, 'data-treatment': f.id }, ...options);
    select.value = (override && override.key) || '';
    const isOther = select.value === WF.TREATMENT_OTHER;
    return h('div', { class: 'treatment-control' },
      h('label', { for: sid }, 'Reviewer treatment', h('span', { class: 'visually-hidden', text: ` for: ${f.title}` })),
      select,
      isOther ? h('label', { class: 'visually-hidden', for: `${sid}-text`, text: `Describe the treatment for: ${f.title}` }) : null,
      isOther ? h('input', {
        id: `${sid}-text`, type: 'text', maxlength: 300, 'data-treatment-text': f.id,
        placeholder: 'Describe the treatment', value: (override && override.text) || '',
      }) : null);
  }

  // A screening suggestion from the Paul Hastings framework (see workflow.js), never presented as advice.
  // On a live serious flag the reviewer can replace it; both are shown so the report says who chose what.
  function treatmentNode(f) {
    if (!WF) return null;
    const override = state.treatments.get(f.id);
    const t = WF.treatmentFor(f, override);
    const suggested = h('p', { class: 'treatment' }, h('b', { text: 'Suggested treatment: ' }), `${t.suggested.remedy}. ${t.suggested.basis}`);
    if (!isSerious(f) || state.dismissed.has(f.id)) return suggested;
    return h('div', { class: 'treatment-box' },
      suggested,
      h('p', { class: 'treatment treatment-reviewer', hidden: !t.reviewer },
        h('b', { text: 'Reviewer: ' }), h('span', { text: reviewerLineText(t) })),
      treatmentSelect(f, t, override));
  }

  function refreshReviewerLine(id) {
    const f = state.result.findings.find((x) => x.id === id);
    const box = document.getElementById(`f-${id}`);
    const line = box && box.querySelector('.treatment-reviewer');
    if (!f || !line) return;
    const t = WF.treatmentFor(f, state.treatments.get(id));
    line.hidden = !t.reviewer;
    line.querySelector('span').textContent = reviewerLineText(t);
  }

  function setTreatment(id, key) {
    const next = new Map(state.treatments);
    if (key) next.set(id, { key, text: (state.treatments.get(id) || {}).text || '' });
    else next.delete(id);
    state.treatments = next;
    renderFindings();
    applySelection();
    const select = document.getElementById(`tr-${safeId(id)}`);
    if (select) select.focus(); // arrow keys fire change on each step, so focus stays on the select
  }

  function setTreatmentText(id, text) {
    const next = new Map(state.treatments);
    next.set(id, { key: WF.TREATMENT_OTHER, text });
    state.treatments = next;
    refreshReviewerLine(id);
  }

  function findingNode(f) {
    const dismissed = state.dismissed.has(f.id);
    const confirmed = state.confirmed.has(f.id);
    const multiDoc = state.result.stats.documents > 1;
    const hasSpans = f.spans.length > 0;
    return h('li', {
      class: `finding sev-${f.severity}${dismissed ? ' is-dismissed' : ''}${state.selected === f.id ? ' is-selected' : ''}`,
      id: `f-${f.id}`, 'data-id': f.id,
    },
      h('div', { class: 'finding-head' },
        h('span', { class: 'sev-tag', text: SEV_LABEL[f.severity] }),
        h('span', { class: 'lens-tag', text: f.lensName }),
        f.source === 'ai' ? h('span', { class: 'ai-tag', title: 'Found by the AI deep read. The quote was verified in your document.', text: 'AI read' }) : null,
        f.source === 'web' ? h('span', { class: 'ai-tag', title: 'Found by searching public sources. Confirm each source before relying on it.', text: 'Web' }) : null,
        multiDoc && f.places ? h('span', { class: 'places', text: `in ${f.places}` }) : null,
        dismissed ? h('span', { class: 'places', text: 'Dismissed' }) : null,
        confirmed && !dismissed ? h('span', { class: 'confirmed-tag', text: 'Confirmed' }) : null),
      h('h3', { text: f.title }),
      quoteNode(f.quote),
      f.detail ? h('p', { class: 'detail', text: f.detail }) : null,
      treatmentNode(f),
      f.adjudicated ? h('p', { class: 'detail', text: `Senior review (Ultra): ${f.adjudicationNote || 'confirmed.'}` }) : null,
      sourcesNode(f),
      f.count > 1 ? h('p', { class: 'repeat', text: `Appears ${f.count} times.` }) : null,
      !hasSpans ? h('p', { class: 'repeat', text: 'Applies to the document as a whole.' }) : null,
      h('details', null,
        h('summary', { text: 'Why it matters and what to ask' }),
        h('p', null, h('b', { text: 'Why it matters. ' }), f.why),
        h('p', null, h('b', { text: 'Ask. ' }), f.ask)),
      h('div', { class: 'print-only' },
        h('p', null, h('b', { text: 'Why it matters. ' }), f.why),
        h('p', null, h('b', { text: 'Ask. ' }), f.ask)),
      h('div', { class: 'finding-actions' },
        hasSpans ? h('button', { type: 'button', class: 'link', 'data-act': 'show', text: 'Show in document' }) : null,
        dismissed ? null : h('button', {
          type: 'button', class: 'link', 'data-act': 'confirm', 'aria-pressed': String(confirmed),
          text: confirmed ? 'Unconfirm' : 'Confirm',
        }),
        h('button', { type: 'button', class: 'link', 'data-act': 'dismiss', text: dismissed ? 'Restore' : 'Dismiss' })));
  }

  function renderFindings() {
    const items = visibleFindings();
    $('finding-list').replaceChildren(...items.map(findingNode));
    const empty = $('finding-empty');
    empty.hidden = items.length > 0;
    if (!items.length) {
      empty.textContent = state.result.findings.length
        ? 'Nothing matches these filters. Choose All to see every finding.'
        : 'No flags from these checks. That is not a clean bill of health: it only means none of the patterns it looks for appeared. Try adding the underlying records, or widening the areas you check.';
    }
  }

  function renderFollowups() {
    const list = followups();
    $('followup-list').replaceChildren(...(list.length
      ? list.map((x) => h('li', { class: `sev-${x.severity}` },
        h('p', { class: 'ask', text: x.ask }),
        h('p', { class: 'because', text: `Because: ${x.titles.join('; ')}` })))
      : [h('li', { class: 'sev-low' }, h('p', { class: 'ask', text: 'Nothing to ask for yet.' }))]));
  }

  /* ---------------------------------------------------------------- */
  /* connections pane                                                  */
  /* ---------------------------------------------------------------- */

  function connSection(title, intro, items) {
    if (!items.length) return null;
    return h('section', { class: 'conn-section' },
      h('h3', { text: title }),
      intro ? h('p', { text: intro }) : null,
      h('ul', { class: 'conn-list' }, ...items));
  }

  function connItem(label, meta, warn) {
    return h('li', { class: warn ? 'warn' : '' },
      h('div', { class: 'label', text: label }),
      meta ? h('div', { class: 'meta', text: meta }) : null);
  }

  function renderConnections() {
    const root = $('connections');
    const d = state.dossier;
    if (!d) { root.replaceChildren(h('p', { text: 'Connections are unavailable.' })); return; }
    const names = (idx) => idx.map((i) => (state.analyzed[i] ? state.analyzed[i].name : `Document ${i + 1}`)).join(', ');
    const sections = [];

    const modeNote = {
      thin: 'Little material was supplied, so most cross-checks could not run. The list under "What could not be tested" says what would change that.',
      many: `${d.profile.documents >= DS.MANY_DOCS ? 'Many documents' : 'Many findings'}: repeated problems are grouped and the documents are ordered by how much they need your attention.`,
      normal: '',
    }[d.profile.mode];
    if (modeNote) sections.push(h('p', { class: 'conn-note', text: modeNote }));

    const unrelated = d.conflict_scope.filter((c) => !c.sharedParty);
    sections.push(connSection('Figure conflicts to read with care',
      'These documents state different figures but share no named party, so the difference may just be two different organisations.',
      unrelated.map((c) => connItem(c.title, names(c.documents), true))));

    sections.push(connSection('Parties that appear in more than one document', null,
      d.entities.filter((e) => e.documents.length > 1).map((e) => connItem(e.name, `${names(e.documents)}`))));
    sections.push(connSection('Same amount in different documents',
      'Check these describe the same thing and tie out.',
      d.shared_figures.map((f) => connItem(f.amount, names(f.documents)))));
    sections.push(connSection('Findings that touch the same party', null,
      d.threads.map((t) => connItem(t.entity, t.findings.map((f) => f.title).join('; ')))));
    sections.push(connSection('Repeated across documents', null,
      d.themes.filter((t) => t.systemic).map((t) => connItem(t.title, `${plural(t.documents, 'document', 'documents')}, ${plural(t.occurrences, 'occurrence', 'occurrences')}`, t.severity === 'critical' || t.severity === 'high'))));
    sections.push(connSection('Read these first', null,
      d.read_first.filter((x) => x.score > 0).map((x) => connItem(x.name, `attention score ${x.score}`))));
    sections.push(connSection('Near-duplicate documents', 'Review one of each pair.',
      d.duplicates.map((p) => connItem(`${state.analyzed[p.a].name} and ${state.analyzed[p.b].name}`, `${Math.round(p.similarity * 100)}% the same`))));
    sections.push(connSection('Referred to but not supplied', null,
      d.gaps.filter((g) => g.type === 'referenced-not-supplied').map((g) => connItem(g.item, g.why, true))));
    const req = d.requirements;
    sections.push(connSection(
      req && req.engagement ? `What this file still needs (read as: ${req.engagement})` : 'What this file still needs',
      'Worked out by a reasoning model from what you supplied, not from a fixed list. Check it against your own judgement.',
      d.gaps.filter((g) => g.type === 'required-by-reasoning').map((g) => connItem(
        `${g.item}${g.priority === 'high' ? ' (high priority)' : ''}`,
        [g.whatItIs ? `Not a standard name, so described: ${g.whatItIs}` : '', g.why, g.blocks].filter(Boolean).join(' '),
        g.priority === 'high'))));
    sections.push(connSection('What could not be tested', null,
      d.gaps.filter((g) => g.type === 'single-source').map((g) => connItem(g.item, g.why))));
    sections.push(connSection('Timeline', null,
      d.timeline.slice(0, 20).map((e) => connItem(e.date, `${state.analyzed[e.doc].name}: ${e.snippet}`))));

    const real = sections.filter(Boolean);
    root.replaceChildren(...(real.length ? real : [h('p', { text: 'Nothing to connect yet.' })]));
    const count = d.entities.filter((e) => e.documents.length > 1).length + d.shared_figures.length + d.gaps.length;
    $('n-connections').textContent = `(${count})`;
  }

  /* ---------------------------------------------------------------- */
  /* workflow: request list, Q&A log, coverage, reviewer sign-off       */
  /* ---------------------------------------------------------------- */

  const REVIEWER_KEY = 'fineprint-reviewer';
  function storedReviewer() {
    try { return localStorage.getItem(REVIEWER_KEY) || ''; } catch (err) { return ''; }
  }
  function storeReviewer(value) {
    try { localStorage.setItem(REVIEWER_KEY, value); } catch (err) { /* storage blocked: the name is kept in memory only */ }
  }

  function isoToday() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  }

  const requestRows = () => (WF && state.dossier
    ? WF.applyRequestStatus(WF.irlRows(state.dossier, activeFindings(), isoToday()), state.requestStatus) : []);
  const questionRows = () => (WF ? WF.qaRows(state.result.findings, { dismissed: state.dismissed, memoRefs: state.memoRefs || [] }) : []);
  const reportState = () => (WF ? WF.reportStatus({
    findings: state.result.findings, dismissed: state.dismissed, confirmed: state.confirmed, reviewer: state.reviewer, date: isoToday(),
    requests: requestRows(),
  }) : null);

  function renderStatus() {
    const s = reportState();
    const el = $('report-status');
    el.textContent = s ? s.label : '';
    el.classList.toggle('is-final', Boolean(s && s.final));
  }

  // columns: [key, header, numeric?]
  function tableNode(columns, rows, rowClass) {
    return h('div', { class: 'wf-table-wrap' },
      h('table', { class: 'wf-table' },
        h('thead', null, h('tr', null, ...columns.map(([, label]) => h('th', { scope: 'col', text: label })))),
        h('tbody', null, ...rows.map((r) => h('tr', { class: rowClass ? rowClass(r) : '' },
          ...columns.map(([key, , numeric]) => h('td', { class: numeric ? 'num' : '', text: String(r[key] ?? '') })))))));
  }

  const isEmptyWorkstream = (r) => (r.documents || r.findings || r.outstanding ? '' : 'is-empty');

  function remedySourceNode() {
    return h('p', { class: 'wf-source' },
      'Suggested treatments follow the buyer-side framework in ',
      h('a', { href: WF.REMEDY_SOURCE, target: '_blank', rel: 'noopener noreferrer', text: 'Paul Hastings, "Legal due diligence: what really matters"' }),
      '. They are screening suggestions to discuss with your advisers, not legal advice.');
  }

  const NOTE_HINT = { Received: 'Add a note saying where it was received.', 'Not applicable': 'Add a note giving the reason.' };
  const resolvedCount = (requests) => requests.filter((r) => r.resolved).length;
  const requestHeading = (requests) => `Request list (${requests.length}; ${resolvedCount(requests)} resolved by reviewer)`;

  function coverageTable(requests) {
    const cov = WF.coverage(state.dossier, activeFindings(), requests);
    return tableNode([['workstream', 'Workstream'], ['documents', 'Documents', true], ['findings', 'Findings', true], ['outstanding', 'Open requests', true]], cov, isEmptyWorkstream);
  }

  function requestHintText(r) {
    if (r.pending) return `${NOTE_HINT[r.pending]} Until then it stays ${r.status.toLowerCase()}.`;
    return r.statusBy === 'Reviewer' ? 'Set by the reviewer.' : '';
  }

  function requestControls(r, i) {
    const override = state.requestStatus.get(r.key) || {};
    const select = h('select', {
      id: `rq-status-${i}`, 'data-req-key': r.key, 'aria-label': `Status of request ${r.ref}`, 'aria-describedby': `rq-hint-${i}`,
    },
      ...WF.REQUEST_STATUSES.map((s) => h('option', { value: s, text: s })));
    select.value = override.status || r.status;
    const note = h('input', {
      id: `rq-note-${i}`, type: 'text', maxlength: 300, 'data-req-note': r.key, value: override.note || '',
      'aria-label': `Note for request ${r.ref}`, 'aria-describedby': `rq-hint-${i}`,
      placeholder: NOTE_HINT[select.value] ? 'Required: where received, or why' : 'Optional note',
    });
    return [
      h('td', { 'data-label': 'Status' }, select),
      h('td', { 'data-label': 'Reviewer note' }, note, h('span', { class: 'req-hint', id: `rq-hint-${i}`, text: requestHintText(r) })),
    ];
  }

  // data-label lets the narrow layout stack each row as a card with its own field names.
  function requestTable(requests) {
    const head = ['Ref', 'Workstream', 'Request', 'Priority', 'Status', 'Reviewer note'];
    const cell = (label, text) => h('td', { 'data-label': label, text });
    return h('div', { class: 'wf-table-wrap' },
      h('table', { class: 'wf-table wf-requests' },
        h('thead', null, h('tr', null, ...head.map((label) => h('th', { scope: 'col', text: label })))),
        h('tbody', null, ...requests.map((r, i) => h('tr', { class: r.resolved ? 'is-resolved' : '' },
          cell('Ref', r.ref), cell('Workstream', r.workstream), cell('Request', r.request), cell('Priority', r.priority),
          ...requestControls(r, i))))));
  }

  // After a note is typed: refresh counts, hints and coverage without rebuilding the inputs, so focus stays put.
  function refreshRequestCounts() {
    const requests = requestRows();
    const head = $('wf-requests-head');
    if (head) head.textContent = requestHeading(requests);
    const cov = $('wf-coverage');
    if (cov) cov.replaceChildren(coverageTable(requests));
    requests.forEach((r, i) => {
      const hint = $(`rq-hint-${i}`);
      if (hint) hint.textContent = requestHintText(r);
      const row = hint && hint.closest('tr');
      if (row) row.classList.toggle('is-resolved', r.resolved);
    });
    renderStatus();
  }

  function setRequestStatus(key, patch) {
    const current = state.requestStatus.get(key) || {};
    state.requestStatus = new Map(state.requestStatus).set(key, Object.assign({}, current, patch));
  }

  function onRequestStatusChange(select) {
    const key = select.dataset.reqKey;
    setRequestStatus(key, { status: select.value });
    renderWorkflow();
    renderStatus();
    // Focus stays on the select (arrow keys fire change on each step); the hint it describes says when a note is needed.
    const again = Array.from(document.querySelectorAll('#workflow select[data-req-key]')).find((s) => s.dataset.reqKey === key);
    if (again) again.focus();
  }

  function renderWorkflow() {
    const root = $('workflow');
    if (!WF || !state.dossier) { root.replaceChildren(h('p', { text: 'The workflow view is unavailable.' })); return; }
    const requests = requestRows();
    const questions = questionRows();
    root.replaceChildren(
      h('section', { class: 'conn-section' },
        h('h3', { text: 'Coverage by workstream' }),
        h('p', { text: 'What each workstream has to go on. A workstream with no documents has not been reviewed at all. Resolved requests are not counted as open.' }),
        h('div', { id: 'wf-coverage' }, coverageTable(requests))),
      h('section', { class: 'conn-section' },
        h('h3', { id: 'wf-requests-head', text: requestHeading(requests) }),
        requests.length
          ? [h('p', { text: 'Set each request\'s status as it comes in. Received and Not applicable need a note and then count as resolved.' }), requestTable(requests)]
          : h('p', { text: 'Nothing to request yet. Without "Work out what this file still needs", only documents referred to but not supplied are listed.' })),
      h('section', { class: 'conn-section' },
        h('h3', { text: `Q&A log (${questions.length})` }),
        h('p', { text: 'One row per distinct question from the follow-up list, with the finding it rests on and a citation. Download it to send or track.' })),
      remedySourceNode());
    $('n-workflow').textContent = `(${requests.length})`;
  }

  function downloadCsv(kind, rows, columns) {
    const blob = new Blob(['﻿', WF.toCsv(rows, columns)], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = h('a', { href: url, download: `fine-print-${kind}-${isoToday()}.csv`, hidden: true });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast(`${plural(rows.length, 'row', 'rows')} downloaded.`);
  }

  function toggleConfirm(id) {
    if (state.confirmed.has(id)) state.confirmed.delete(id);
    else state.confirmed.add(id);
    renderStatus();
    renderFindings();
    applySelection();
  }

  /* ---------------------------------------------------------------- */
  /* document pane                                                     */
  /* ---------------------------------------------------------------- */

  function renderDocTabs() {
    const docs = state.analyzed;
    $('doc-tabs').replaceChildren(...(docs.length > 1 ? docs.map((d, i) => h('button', {
      type: 'button', class: 'doc-tab', title: d.name, 'aria-pressed': String(i === state.activeDoc), text: d.name,
      onclick: () => { state.activeDoc = i; renderDocTabs(); renderSheet(); },
    })) : []));
  }

  function renderSheet() {
    const doc = state.analyzed[state.activeDoc];
    const sheet = $('sheet');
    if (!doc) { sheet.replaceChildren(); return; }
    const spans = [];
    activeFindings().forEach((f) => f.spans.forEach((s) => {
      if (s.doc === state.activeDoc) spans.push({ start: s.start, end: s.end, f });
    }));
    const cuts = new Set([0, doc.text.length]);
    spans.forEach((s) => { cuts.add(s.start); cuts.add(s.end); });
    const points = [...cuts].sort((a, b) => a - b);
    const rank = { critical: 0, high: 1, medium: 2, low: 3 };
    const nodes = [];
    for (let i = 0; i < points.length - 1; i += 1) {
      const a = points[i];
      const b = points[i + 1];
      const text = doc.text.slice(a, b);
      const cover = spans.filter((s) => s.start <= a && s.end >= b);
      if (!cover.length) { nodes.push(text); continue; }
      const top = cover.reduce((x, y) => (rank[x.f.severity] <= rank[y.f.severity] ? x : y));
      const ids = [...new Set(cover.map((s) => s.f.id))];
      nodes.push(h('mark', {
        class: `sev-${top.f.severity}${state.selected && ids.includes(state.selected) ? ' is-active' : ''}`,
        'data-ids': ids.join(' '), 'data-a': a, 'data-b': b, tabindex: '0', title: top.f.title, text,
      }));
    }
    sheet.replaceChildren(...nodes);
  }

  function markFor(id, span) {
    return Array.from($('sheet').querySelectorAll('mark')).find((m) => (
      m.dataset.ids.split(' ').includes(id) && +m.dataset.a < span.end && +m.dataset.b > span.start
    ));
  }

  function applySelection() {
    document.querySelectorAll('.finding.is-selected').forEach((n) => n.classList.remove('is-selected'));
    document.querySelectorAll('#sheet mark.is-active').forEach((n) => n.classList.remove('is-active'));
    if (!state.selected) return;
    const item = $(`f-${state.selected}`);
    if (item) item.classList.add('is-selected');
    $('sheet').querySelectorAll('mark').forEach((m) => {
      if (m.dataset.ids.split(' ').includes(state.selected)) m.classList.add('is-active');
    });
  }

  /* ---------------------------------------------------------------- */
  /* memo: drafted by the model, every point traced to a finding        */
  /* ---------------------------------------------------------------- */

  const MEMO_MAX_FINDINGS = 40;

  function memoInputs() {
    const order = (f) => FP.RANK[f.severity] * 1e9 + f.firstPos;
    const findings = [...activeFindings()].sort((a, b) => order(a) - order(b)).slice(0, MEMO_MAX_FINDINGS);
    const d = state.dossier;
    const needs = d ? d.gaps.filter((g) => g.type === 'required-by-reasoning' || g.type === 'referenced-not-supplied') : [];
    return {
      refs: findings.map((f) => f.id),
      body: {
        findings: findings.map((f) => ({
          severity: f.severity, title: f.title.slice(0, 300), where: (f.places || '').slice(0, 600),
          quote: f.quote ? f.quote.text.slice(0, 1500) : '', detail: (f.detail || '').slice(0, 1000),
        })),
        needs: needs.slice(0, 20).map((g) => ({ item: g.item.slice(0, 400), why: (g.why || '').slice(0, 800) })),
        engagement: d && d.requirements ? (d.requirements.engagement || '').slice(0, 600) : '',
      },
    };
  }

  function renderMemo() {
    const box = $('memo');
    const m = state.memo;
    if (!m) { box.hidden = true; box.replaceChildren(); return; }
    if (m.loading) {
      box.replaceChildren(h('p', { text: 'Nemotron is drafting the memo from the verified findings…' }));
      box.hidden = false;
      return;
    }
    if (!m.available) {
      box.replaceChildren(h('p', { text: `No memo: ${m.reason}` }));
      box.hidden = false;
      return;
    }
    const cite = (c) => {
      const n = Number(c.slice(1)) - 1;
      if (c[0] === 'F' && state.memoRefs[n]) {
        return h('button', { type: 'button', class: 'cite', text: c, title: 'Show this finding in the document', onclick: () => showInDocument(state.memoRefs[n]) });
      }
      return h('span', { class: 'cite', text: c, title: c[0] === 'N' ? 'An item still needed' : '' });
    };
    const dropped = m.dropped || {};
    const removed = (dropped.uncited || 0) + (dropped.unsupported_numbers || 0);
    box.replaceChildren(...[
      h('h3', { text: 'Memo' }),
      ...m.sections.map((sec) => h('div', { class: 'memo-section' },
        h('h4', { text: sec.heading }),
        h('ul', null, ...sec.points.map((p) => h('li', null, `${p.text} `, ...p.cites.map(cite)))))),
      h('p', { class: 'memo-note', text: `Drafted by ${String(m.model || '').replace(/^nvidia\//i, '')} for $${(m.cost_usd || 0).toFixed(4)}. Each point cites the findings it rests on (F) or the items still needed (N).${removed ? ` ${plural(removed, 'point was', 'points were')} removed because ${removed === 1 ? 'it' : 'they'} cited nothing real or used a number not in the evidence.` : ''}` }),
    ]);
    box.hidden = false;
  }

  async function draftMemo() {
    const { refs, body } = memoInputs();
    state.memoRefs = refs;
    state.memo = { loading: true };
    renderMemo();
    const headers = { 'Content-Type': 'application/json', Accept: 'application/json' };
    const code = state.ai.authRequired ? $('ai-code').value.trim() : '';
    if (code) headers['X-Review-Token'] = code;
    try {
      const res = await fetch('/api/review/memo', { method: 'POST', headers, body: JSON.stringify(body) });
      const out = await res.json().catch(() => ({}));
      state.memo = res.ok ? out : { available: false, reason: typeof out.detail === 'string' ? out.detail : `the server answered ${res.status}.` };
    } catch (err) {
      state.memo = { available: false, reason: 'the server could not be reached.' };
    }
    renderMemo();
  }

  function showInDocument(id) {
    const f = state.result.findings.find((x) => x.id === id);
    if (!f || !f.spans.length) return;
    const idx = id in state.cursor ? (state.cursor[id] + 1) % f.spans.length : 0;
    state.cursor[id] = idx;
    const span = f.spans[idx];
    state.selected = id;
    if (span.doc !== state.activeDoc) {
      state.activeDoc = span.doc;
      renderDocTabs();
    }
    renderSheet();
    applySelection();
    setView('document');
    requestAnimationFrame(() => {
      const mark = markFor(id, span);
      if (!mark) return;
      mark.scrollIntoView({ block: 'center', behavior: reduceMotion.matches ? 'auto' : 'smooth' });
      mark.classList.add('pulse');
      setTimeout(() => mark.classList.remove('pulse'), 1000);
    });
  }

  function selectFromMark(mark) {
    const ids = mark.dataset.ids.split(' ');
    const at = ids.indexOf(state.selected);
    const id = ids[(at + 1) % ids.length];
    state.selected = id;
    if (!visibleFindings().some((f) => f.id === id)) {
      state.severity = 'all';
      state.lens = 'all';
      renderFilters();
      renderFindings();
    }
    applySelection();
    setView('findings');
    const item = $(`f-${id}`);
    if (item) item.scrollIntoView({ block: 'nearest', behavior: reduceMotion.matches ? 'auto' : 'smooth' });
  }

  /* ---------------------------------------------------------------- */
  /* views, actions                                                    */
  /* ---------------------------------------------------------------- */

  function setView(view) {
    const next = !narrow.matches && view === 'document' ? 'findings' : view;
    state.view = next;
    $('split').dataset.view = next;
    document.querySelectorAll('.tabs [role="tab"]').forEach((t) => {
      const on = t.dataset.view === next;
      t.setAttribute('aria-selected', String(on));
      t.tabIndex = on ? 0 : -1;
    });
  }

  function toggleDismiss(id) {
    if (state.dismissed.has(id)) state.dismissed.delete(id);
    else state.dismissed.add(id);
    if (state.selected === id && state.dismissed.has(id)) state.selected = null;
    renderSummary();
    renderFilters();
    renderFindings();
    renderFollowups();
    renderWorkflow();
    renderSheet();
    applySelection();
  }

  function reportRedFlags(active) {
    if (!WF) return [];
    const serious = active.filter(isSerious);
    if (!serious.length) return [];
    const set = serious.filter((f) => WF.treatmentFor(f, state.treatments.get(f.id)).reviewer).length;
    const lines = [`## Red flags (${serious.length})`, '',
      `"Suggested" treatments are keyword-based screening suggestions following Paul Hastings, "Legal due diligence: what really matters" (${WF.REMEDY_SOURCE}); they are not legal advice.`,
      `"Reviewer" lines are treatments set by the reviewer and replace the suggestion (${set} of ${serious.length} flags).`, ''];
    serious.forEach((f) => {
      const t = WF.treatmentFor(f, state.treatments.get(f.id));
      const confirmed = state.confirmed.has(f.id) ? ', confirmed' : ', not yet confirmed';
      lines.push(`- ${SEV_LABEL[f.severity]}: ${f.title}${f.places ? ` (${f.places})` : ''}${confirmed}`);
      t.lines.forEach((l) => lines.push(`  ${l}`));
    });
    lines.push('');
    return lines;
  }

  function reportAudit() {
    if (!WF) return [];
    const trail = WF.auditTrail({
      findings: state.result.findings, dismissed: state.dismissed, confirmed: state.confirmed,
      treatments: state.treatments, requests: requestRows(), reviewer: state.reviewer, date: isoToday(),
    });
    return ['## Reviewer audit trail', '', ...trail.lines.map((l) => `- ${l}`), ''];
  }

  function reportCoverage() {
    if (!WF || !state.dossier) return [];
    const rows = WF.coverage(state.dossier, activeFindings(), requestRows());
    return ['## Coverage by workstream', '',
      '| Workstream | Documents | Findings | Open requests |', '| --- | ---: | ---: | ---: |',
      ...rows.map((r) => `| ${r.workstream} | ${r.documents} | ${r.findings} | ${r.outstanding} |`), ''];
  }

  function reportRequests(d) {
    const rows = requestRows();
    if (!rows.length) return [];
    const lines = ['## Request list', '', `Resolved by reviewer: ${rows.filter((r) => r.resolved).length} of ${rows.length}`, ''];
    if (d.requirements && d.requirements.engagement) lines.push(`Read as: ${d.requirements.engagement}`, '');
    rows.forEach((r) => {
      const by = r.statusBy === 'Reviewer' ? ' (set by reviewer)' : '';
      lines.push(`${r.ref}. ${r.request} (${r.workstream}; ${r.priority.toLowerCase()} priority; ${r.status.toLowerCase()}${by}; ${r.source.toLowerCase()})`);
      if (r.note) lines.push(`   Reviewer note: ${r.note}`);
      if (r.why) lines.push(`   Why: ${r.why}`);
    });
    lines.push('');
    return lines;
  }

  function buildReport() {
    const active = activeFindings();
    const verdict = FP.verdictFor(active);
    const status = reportState();
    const lines = [
      '# Fine Print report',
      '',
      ...(status ? [`Status: ${status.label}`] : []),
      `Date: ${new Date().toLocaleDateString()}`,
      `Documents: ${state.analyzed.map((d) => d.name).join(', ')}`,
      `Result: ${verdict.headline}`,
      `Findings: ${FP.SEVERITIES.map((s) => `${verdict.counts[s]} ${s}`).join(', ')}`,
      '',
    ];
    const d = state.dossier;
    if (d && d.clearance) {
      lines.push('## Can this file be relied on?', '', d.clearance.headline);
      d.clearance.reasons.filter((r) => !/^Still needed|^Referred to but not supplied/.test(r)).forEach((r) => lines.push(`- ${r}`));
      lines.push('');
    }
    if (state.memo && state.memo.available) {
      lines.push('## Memo', '');
      state.memo.sections.forEach((sec) => {
        lines.push(`### ${sec.heading}`);
        sec.points.forEach((p) => lines.push(`- ${p.text} [${p.cites.join(', ')}]`));
        lines.push('');
      });
    }
    lines.push(...reportRedFlags(active), ...reportCoverage(), ...(d ? reportRequests(d) : []));
    for (const sev of FP.SEVERITIES) {
      const group = active.filter((f) => f.severity === sev);
      if (!group.length) continue;
      lines.push(`## ${SEV_LABEL[sev]} (${group.length})`, '');
      for (const f of group) {
        lines.push(`### ${f.title}`, `- Area: ${f.lensName}`);
        if (f.places) lines.push(`- Where: ${f.places}`);
        if (f.source === 'ai') lines.push('- Source: AI deep read (quote verified in the document)');
        if (f.source === 'web') lines.push('- Source: public web sources (confirm before relying on them)');
        if (f.adjudicated) lines.push(`- Senior review (Ultra): ${f.adjudicationNote || 'confirmed'}`);
        (f.sources || []).filter((s) => /^https?:\/\//i.test(s.url || '')).forEach((s) => lines.push(`- Link: ${s.title || s.url} ${s.url}`));
        if (f.quote) lines.push(`- Evidence: "${f.quote.text.replace(/\s+/g, ' ')}"`);
        if (f.detail) lines.push(`- Detail: ${f.detail}`);
        lines.push(`- Why it matters: ${f.why}`, `- Ask: ${f.ask}`, '');
      }
    }
    lines.push(...reportAudit());
    lines.push('---', 'Pattern-based screening. It points at what to ask and does not prove anything. Not legal, financial or professional advice.');
    return lines.join('\n');
  }

  async function copyText(text, doneMessage) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (err) {
      const ta = h('textarea', { 'aria-hidden': 'true', tabindex: '-1' });
      ta.value = text;
      document.body.append(ta);
      ta.select();
      const ok = document.execCommand && document.execCommand('copy');
      ta.remove();
      if (!ok) { toast('Copy was blocked by the browser. Use Print or save as PDF instead.'); return; }
    }
    toast(doneMessage);
  }

  function showResults() {
    $('setup').hidden = true;
    $('results').hidden = false;
    state.dossier = DS ? DS.build(state.analyzed, state.result.findings, { requirements: state.requirements }) : null;
    state.memo = null;
    $('memo-btn').hidden = !(state.ai.available && state.ai.live);
    renderMemo();
    renderSummary();
    renderFilters();
    renderFindings();
    renderFollowups();
    renderConnections();
    renderWorkflow();
    renderDocTabs();
    renderSheet();
    setView('findings');
    window.scrollTo({ top: 0 });
    const v = $('verdict');
    v.tabIndex = -1;
    v.focus({ preventScroll: true });
  }

  /* ---------------------------------------------------------------- */
  /* AI deep read (server side, Nemotron on Nebius)                    */
  /* ---------------------------------------------------------------- */

  const CODE_KEY = 'fineprint-access-code';
  function storedCode() {
    try { return sessionStorage.getItem(CODE_KEY) || ''; } catch (err) { return ''; }
  }
  function storeCode(value) {
    try { sessionStorage.setItem(CODE_KEY, value); } catch (err) { /* private mode: the code just isn't remembered */ }
  }

  async function setupAi() {
    let s;
    try {
      const res = await fetch('/api/review/status', { headers: { Accept: 'application/json' } });
      if (!res.ok) return;
      s = await res.json();
    } catch (err) {
      return; // opened from disk or a plain static server: the optional reads are simply not offered
    }
    state.ai = {
      available: true,
      live: Boolean(s.live),
      model: String(s.model || ''),
      web: Boolean(s.web),
      authRequired: Boolean(s.auth_required),
    };
    $('ai-opt').hidden = false;
    $('needs-toggle').disabled = !state.ai.live;
    $('needs-toggle').checked = state.ai.live;
    $('ai-toggle').disabled = !state.ai.live;
    $('web-toggle').disabled = !state.ai.web;
    $('funnel-row').hidden = !state.ai.live;

    const hints = [];
    hints.push(state.ai.live
      ? 'Working out what the file needs sends the opening and headings of each document to the model; it is what stops an empty result reading as an all-clear. The AI read checks candidate clauses across the whole document. Every AI finding quotes your document and is discarded if the quote is not really there.'
      : 'The AI options are unavailable: the server has no Nebius API key.');
    hints.push(state.ai.web
      ? 'The web check searches public sources for the companies named in your documents.'
      : 'The web check is unavailable: the server has no Tavily API key.');
    if (state.ai.live || state.ai.web) {
      hints.push('Each ticked option sends document text to this server, and the Nemotron options send it on to Nebius. With every option unticked nothing leaves your browser.');
    }
    $('ai-hint').textContent = hints.join(' ');

    if (state.ai.authRequired && (state.ai.live || state.ai.web)) {
      $('ai-code-row').hidden = false;
      $('ai-code').value = storedCode();
      $('ai-code').addEventListener('change', (e) => storeCode(e.target.value.trim()));
    }
    const refreshPrivacy = () => {
      const on = $('needs-toggle').checked || $('ai-toggle').checked || $('web-toggle').checked;
      $('privacy').textContent = on ? PRIVACY_AI : PRIVACY_LOCAL;
    };
    $('needs-toggle').addEventListener('change', refreshPrivacy);
    refreshPrivacy();
    $('ai-toggle').addEventListener('change', refreshPrivacy);
    $('web-toggle').addEventListener('change', refreshPrivacy);
  }

  function optionalReadsRequested() {
    const ai = state.ai.live && $('ai-toggle').checked;
    return {
      ai,
      web: state.ai.web && $('web-toggle').checked,
      funnel: ai && $('funnel-toggle').checked,
      needs: state.ai.live && $('needs-toggle').checked,
    };
  }

  async function deepRead(docs, reads) {
    const sent = docs.slice(0, AI_MAX_DOCS).map((d) => ({ name: d.name, text: d.text.slice(0, AI_MAX_CHARS) }));
    const headers = { 'Content-Type': 'application/json', Accept: 'application/json' };
    const code = state.ai.authRequired ? $('ai-code').value.trim() : '';
    if (code) {
      headers['X-Review-Token'] = code;
      storeCode(code);
    }
    const res = await fetch('/api/review/deep', {
      method: 'POST',
      headers,
      body: JSON.stringify({
        documents: sent, lenses: [...state.lenses], ai: reads.ai, web: reads.web,
        triage: reads.funnel, adjudicate: reads.funnel,
      }),
    });
    if (!res.ok) {
      let message = 'The optional read failed.';
      try {
        const body = await res.json();
        if (typeof body.detail === 'string') message = body.detail;
        else if (res.status === 422) message = 'The documents are too large for the optional reads (up to 10 documents, 200,000 characters each).';
      } catch (err) { /* keep the generic message */ }
      throw new Error(message);
    }
    const out = await res.json();
    out.partial = docs.length > AI_MAX_DOCS || docs.some((d) => d.text.length > AI_MAX_CHARS);
    return out;
  }

  // What the file still needs, worked out by a reasoning model on the server. Never a fixed list.
  async function fetchRequirements(docs) {
    const headers = { 'Content-Type': 'application/json', Accept: 'application/json' };
    const code = state.ai.authRequired ? $('ai-code').value.trim() : '';
    if (code) headers['X-Review-Token'] = code;
    const sent = docs.slice(0, 12).map((d) => ({ name: d.name, text: d.text.slice(0, 200000) }));
    try {
      const res = await fetch('/api/review/requirements', { method: 'POST', headers, body: JSON.stringify({ documents: sent }) });
      if (!res.ok) {
        let reason = `The server answered ${res.status}.`;
        try { const body = await res.json(); if (typeof body.detail === 'string') reason = body.detail; } catch (err) { /* keep */ }
        return { available: false, reason, engagement: '', items: [] };
      }
      return await res.json();
    } catch (err) {
      return { available: false, reason: 'The server could not be reached.', engagement: '', items: [] };
    }
  }

  function toFinding(raw, docs) {
    const span = raw.spans[0];
    const q = FP.sentenceAround(docs[span.doc].text, span.start, span.end);
    return {
      id: raw.id, source: raw.source || 'ai', lens: raw.lens, lensName: FP.LENS_NAME[raw.lens], severity: raw.severity,
      title: raw.title, why: raw.why, ask: raw.ask, spans: raw.spans, count: 1, detail: '',
      quote: Object.assign({ doc: span.doc }, q), places: docs[span.doc].name, firstPos: span.start,
      sources: Array.isArray(raw.sources) ? raw.sources : [],
      adjudicated: Boolean(raw.adjudicated), adjudicationNote: raw.adjudication_note || '',
    };
  }

  function mergeOptionalReads(result, payload, docs, reads) {
    const overlaps = (a, b) => a.doc === b.doc && a.start < b.end && b.start < a.end;
    const existing = result.findings.flatMap((f) => f.spans);
    const added = [];
    let overlapped = 0;
    for (const raw of payload.findings || []) {
      const span = raw.spans && raw.spans[0];
      if (!span || !docs[span.doc]) continue;
      if (existing.some((s) => overlaps(s, span))) { overlapped += 1; continue; }
      added.push(toFinding(raw, docs));
    }
    const web = payload.web || null;
    let webAdded = 0;
    for (const raw of (web && web.findings) || []) {
      const span = raw.spans && raw.spans[0];
      if (!span || !docs[span.doc]) continue;
      added.push(toFinding(raw, docs));
      webAdded += 1;
    }
    const findings = [...result.findings, ...added].sort((x, y) => (
      FP.RANK[x.severity] - FP.RANK[y.severity]
      || FP.LENS_ORDER[x.lens] - FP.LENS_ORDER[y.lens]
      || x.firstPos - y.firstPos));
    return {
      result: Object.assign({}, result, { findings }),
      summary: {
        ai: reads.ai && !payload.ai_error ? {
          model: state.ai.model.replace(/^nvidia\//i, ''),
          added: added.length - webAdded, overlapped, dropped: payload.dropped || 0,
          truncated: Boolean(payload.truncated || payload.partial),
          failedChunks: payload.failed_chunks || 0,
          tiers: payload.tiers || null, triage: payload.triage || null,
          adjudication: payload.adjudication || null, tokens: payload.tokens || 0, cost: payload.cost_usd || 0,
          clauses: payload.clauses || null,
        } : null,
        aiError: reads.ai ? (payload.ai_error || '') : '',
        web: reads.web ? {
          available: Boolean(web && web.available), reason: (web && web.reason) || '',
          checked: (web && web.checked) || [], added: webAdded, lookups: (web && web.lookups) || [],
        } : null,
        failed: '',
      },
    };
  }

  function run() {
    if ($('paste').value.trim() && !addPasted()) return;
    if (!state.docs.length) return;
    const button = $('run');
    button.disabled = true;
    button.textContent = 'Checking…';
    setTimeout(async () => {
      try {
        state.analyzed = state.docs.map((d) => ({ name: d.name, text: d.text }));
        state.result = FP.analyze(state.analyzed, { lenses: [...state.lenses] });
        state.aiSummary = null;
        state.requirements = null;
        const reads = optionalReadsRequested();
        // Runs whenever the server has a live model, so an empty result can never read as an all-clear by default.
        if (reads.needs) {
          button.textContent = 'Working out what the file needs…';
          state.requirements = await fetchRequirements(state.analyzed);
        }
        if (reads.ai || reads.web) {
          button.textContent = reads.ai ? 'Reading with Nemotron…' : 'Checking public sources…';
          try {
            const payload = await deepRead(state.analyzed, reads);
            const merged = mergeOptionalReads(state.result, payload, state.analyzed, reads);
            state.result = merged.result;
            state.aiSummary = merged.summary;
          } catch (err) {
            state.aiSummary = { failed: err.message };
          }
        }
        state.dismissed = new Set();
        // Confirmations belong to one run: finding ids repeat across runs, so they are never carried over.
        state.confirmed = new Set();
        // Treatments and request statuses are reviewer decisions about this run's output, so they reset with it.
        state.treatments = new Map();
        state.requestStatus = new Map();
        state.memoRefs = [];
        state.severity = 'all';
        state.lens = 'all';
        state.showDismissed = false;
        $('show-dismissed').checked = false;
        state.activeDoc = 0;
        state.selected = null;
        state.cursor = {};
        showResults();
      } catch (err) {
        setStatus(`The check failed: ${err.message}`, true);
      } finally {
        button.textContent = 'Check documents';
        updateRunState();
      }
    }, 30);
  }

  /* ---------------------------------------------------------------- */
  /* wiring                                                            */
  /* ---------------------------------------------------------------- */

  function wireWorkflow() {
    const reviewer = $('reviewer');
    state.reviewer = storedReviewer();
    reviewer.value = state.reviewer;
    reviewer.addEventListener('input', (e) => {
      state.reviewer = e.target.value;
      storeReviewer(state.reviewer.trim());
      if (state.result) renderStatus();
    });
    if (!WF) {
      $('tab-workflow').hidden = true;
      return;
    }
    $('download-irl').addEventListener('click', () => downloadCsv('request-list', requestRows(), WF.IRL_COLUMNS));
    $('download-qa').addEventListener('click', () => downloadCsv('qa-log', questionRows(), WF.QA_COLUMNS));
    const wf = $('workflow');
    wf.addEventListener('change', (e) => { if (e.target.matches('select[data-req-key]')) onRequestStatusChange(e.target); });
    wf.addEventListener('input', (e) => {
      if (!e.target.matches('input[data-req-note]')) return;
      setRequestStatus(e.target.dataset.reqNote, { note: e.target.value });
      refreshRequestCounts();
    });
    const list = $('finding-list');
    list.addEventListener('change', (e) => { if (e.target.matches('select[data-treatment]')) setTreatment(e.target.dataset.treatment, e.target.value); });
    list.addEventListener('input', (e) => {
      if (e.target.matches('input[data-treatment-text]')) setTreatmentText(e.target.dataset.treatmentText, e.target.value);
    });
  }

  function init() {
    if (!FP) {
      $('status').textContent = 'The review engine failed to load. Reload the page.';
      return;
    }
    renderLenses();
    renderDocs();
    updateRunState();
    setupAi();

    $('lens-all').addEventListener('click', () => { FP.LENSES.forEach((l) => state.lenses.add(l.id)); renderLenses(); updateRunState(); });
    $('lens-none').addEventListener('click', () => { state.lenses.clear(); renderLenses(); updateRunState(); });
    $('paste').addEventListener('input', updateRunState);
    $('paste-add').addEventListener('click', addPasted);
    $('memo-btn').addEventListener('click', draftMemo);
    loadExamples();
    $('run').addEventListener('click', run);

    const drop = $('drop');
    const input = $('file-input');
    drop.addEventListener('click', () => input.click());
    drop.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } });
    input.addEventListener('change', () => { handleFiles(input.files).then(() => { input.value = ''; }); });
    ['dragenter', 'dragover'].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add('is-over'); }));
    ['dragleave', 'drop'].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove('is-over'); }));
    drop.addEventListener('drop', (e) => handleFiles(e.dataTransfer.files));
    ['dragover', 'drop'].forEach((t) => window.addEventListener(t, (e) => e.preventDefault()));

    $('back').addEventListener('click', () => {
      $('results').hidden = true;
      $('setup').hidden = false;
      window.scrollTo({ top: 0 });
    });
    $('copy').addEventListener('click', () => copyText(buildReport(), 'Report copied.'));
    $('print').addEventListener('click', () => window.print());
    $('copy-followups').addEventListener('click', () => copyText(
      followups().map((x, i) => `${i + 1}. ${x.ask}`).join('\n'), 'Follow-up list copied.'));
    wireWorkflow();

    $('lens-filter').addEventListener('change', (e) => { state.lens = e.target.value; renderFilters(); renderFindings(); });
    $('show-dismissed').addEventListener('change', (e) => { state.showDismissed = e.target.checked; renderFilters(); renderFindings(); });

    $('finding-list').addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-act]');
      const item = e.target.closest('.finding');
      if (!item || e.target.closest('.treatment-control')) return;
      const id = item.dataset.id;
      if (btn && btn.dataset.act === 'dismiss') { toggleDismiss(id); return; }
      if (btn && btn.dataset.act === 'confirm') { toggleConfirm(id); return; }
      if (btn && btn.dataset.act === 'show') { showInDocument(id); return; }
      if (e.target.closest('summary')) return;
      state.selected = id;
      applySelection();
    });

    const sheet = $('sheet');
    sheet.addEventListener('click', (e) => { const m = e.target.closest('mark'); if (m) selectFromMark(m); });
    sheet.addEventListener('keydown', (e) => {
      const m = e.target.closest && e.target.closest('mark');
      if (m && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); selectFromMark(m); }
    });

    const tabs = Array.from(document.querySelectorAll('.tabs [role="tab"]'));
    tabs.forEach((t) => t.addEventListener('click', () => setView(t.dataset.view)));
    document.querySelector('.tabs').addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      const shown = tabs.filter((t) => t.offsetParent !== null);
      const at = shown.indexOf(document.activeElement);
      if (at === -1) return;
      const next = shown[(at + (e.key === 'ArrowRight' ? 1 : shown.length - 1)) % shown.length];
      next.focus();
      setView(next.dataset.view);
    });
    narrow.addEventListener('change', () => setView(state.view));
  }

  init();
})();
