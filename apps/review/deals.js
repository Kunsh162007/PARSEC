/*
 * Deal folders on the review page. Opens a folder from the deals folder (?deal=Name), checks it, saves the review
 * back into the folder as the reviewer works, exports marked copies, and carries out what the assistant is asked.
 * It uses the page's own state and actions (window.FinePrintApp); it adds no second copy of either.
 */
(function () {
  'use strict';

  const APP = window.FinePrintApp;
  const AS = window.FinePrintAssistant;
  const EQ = window.FinePrintEarnings;
  const FP = window.FinePrint;
  if (!APP || !APP.state || !FP) return;
  const S = APP.state;
  const $ = (id) => document.getElementById(id);
  const enc = encodeURIComponent;

  const SAVE_DELAY_MS = 700;
  const DEFAULT_MULTIPLE = 10;
  const HELP = 'You can say: "open" and a folder name; "what\'s wrong with" and a document; "next problem"; '
    + '"not a problem" to remove a mark; "bring it back"; "show tax problems"; "summary"; "export"; or "list my folders".';

  const deal = { name: '', names: [], saveTimer: 0, pending: null, restoring: false, loading: false, dismissedKeys: [], list: null, cursor: -1, undo: [] };

  /* ---------------------------------------------------------------- */
  /* server                                                            */
  /* ---------------------------------------------------------------- */

  async function api(path, opts) {
    const options = opts || {};
    const res = await fetch(path, Object.assign({}, options, { headers: APP.authHeaders(options.headers) }));
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `the server answered ${res.status}`);
    return body;
  }

  const jsonBody = (method, body) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

  async function loadDealNames() {
    const out = await api('/api/v1/deals');
    deal.names = out.deals.map((d) => d.name);
    deal.root = out.root;
    return out;
  }

  function status(message) {
    $('deal-status').textContent = message || '';
  }

  /* ---------------------------------------------------------------- */
  /* saving                                                            */
  /* ---------------------------------------------------------------- */

  function hash(text) {
    let h = 0x811c9dc5;
    for (let i = 0; i < text.length; i += 1) h = Math.imul(h ^ text.charCodeAt(i), 0x01000193) >>> 0;
    return h.toString(16);
  }

  const fingerprint = (docs) => docs.map((d) => ({ name: d.name, chars: d.text.length, hash: hash(d.text) }));
  const sameFiles = (a, b) => Array.isArray(a) && a.length === b.length && a.every((x, i) => x.name === b[i].name && x.hash === b[i].hash);
  // A removed mark is remembered by what it says and where, so it stays removed when the documents are checked again.
  const keyOf = (f) => `${f.title}|${f.quote && f.quote.text ? f.quote.text : ''}`;

  function snapshot() {
    return {
      version: 1,
      savedAt: new Date().toISOString(),
      documents: fingerprint(S.analyzed),
      lenses: [...S.lenses],
      result: S.result,
      requirements: S.requirements || null,
      aiSummary: S.aiSummary || null,
      earnings: S.earnings || null,
      dismissed: [...S.dismissed],
      dismissedKeys: S.result.findings.filter((f) => S.dismissed.has(f.id)).map(keyOf),
      confirmed: [...S.confirmed],
      treatments: [...S.treatments],
      requestStatus: [...S.requestStatus],
      reviewer: S.reviewer,
    };
  }

  // A save belongs to the deal it was taken from: its name and contents are fixed when it is scheduled.
  async function save(target, review) {
    try {
      await api(`/api/v1/deals/${enc(target)}/review`, jsonBody('PUT', review));
      $('deal-saved').textContent = `Saved ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
    } catch (err) {
      $('deal-saved').textContent = `Not saved: ${err.message}`;
    }
  }

  function scheduleSave() {
    if (!deal.name || !S.result || deal.restoring || deal.loading) return;   // nothing is saved while a folder loads
    clearTimeout(deal.saveTimer);
    $('deal-saved').textContent = 'Saving…';
    deal.pending = { target: deal.name };
    deal.saveTimer = setTimeout(() => {
      const { target } = deal.pending;
      deal.pending = null;
      save(target, snapshot());
    }, SAVE_DELAY_MS);
  }

  // Before another deal is opened, the last changes to this one are saved to this one.
  async function flushSave() {
    if (!deal.pending) return;
    clearTimeout(deal.saveTimer);
    const { target } = deal.pending;
    deal.pending = null;
    await save(target, snapshot());
  }

  function restore(saved) {
    deal.restoring = true;
    S.analyzed = S.docs.map((d) => ({ name: d.name, text: d.text }));
    S.layouts = S.docs.map((d) => d.layout || null);
    S.lenses = new Set(saved.lenses || FP.LENSES.map((l) => l.id));
    S.result = saved.result;
    S.requirements = saved.requirements || null;
    S.aiSummary = saved.aiSummary || null;
    S.earnings = saved.earnings || null;
    S.dismissed = new Set(saved.dismissed || []);
    S.confirmed = new Set(saved.confirmed || []);
    S.treatments = new Map(saved.treatments || []);
    S.requestStatus = new Map(saved.requestStatus || []);
    Object.assign(S, { severity: 'all', lens: 'all', activeDoc: 0, selected: null, noteId: null, cursor: {} });
    APP.showResults();
    deal.restoring = false;
    $('deal-saved').textContent = `Saved review from ${new Date(saved.savedAt).toLocaleString()}`;
  }

  /* ---------------------------------------------------------------- */
  /* after every check: earnings figures, removed marks carried over    */
  /* ---------------------------------------------------------------- */

  const bySeverity = (x, y) => FP.RANK[x.severity] - FP.RANK[y.severity] || x.firstPos - y.firstPos;

  async function addEarnings(button) {
    // A paid AI read: only when the reviewer has ticked one of the AI checks, as the page promises.
    if (!EQ || !EQ.looksLikeEarnings(S.analyzed) || !S.ai.available || !APP.aiAllowed()) return;
    button.textContent = 'Checking the earnings figures…';
    try {
      const out = await api('/api/v1/data-room/run', jsonBody('POST', {
        company_name: deal.name || 'Target company', multiple: DEFAULT_MULTIPLE, documents: EQ.pickDocuments(S.analyzed),
      }));
      const added = EQ.toFindings(out, S.analyzed).map((raw) => APP.toFinding(raw, S.analyzed));
      S.result = Object.assign({}, S.result, { findings: [...S.result.findings, ...added].sort(bySeverity) });
      S.earnings = { line: EQ.summaryLine(out) };
    } catch (err) {
      S.earnings = { line: `The earnings figures could not be checked this time (${err.message}).` };
    }
  }

  APP.onChecked = async (button) => {
    await addEarnings(button);
    if (deal.dismissedKeys.length) {
      const keys = new Set(deal.dismissedKeys);
      S.result.findings.forEach((f) => { if (keys.has(keyOf(f))) S.dismissed.add(f.id); });
    }
    deal.list = null;
    deal.cursor = -1;
    if (deal.name) setTimeout(scheduleSave, 0);   // once the results are on screen
  };
  APP.onChange = scheduleSave;

  /* ---------------------------------------------------------------- */
  /* opening a folder                                                  */
  /* ---------------------------------------------------------------- */

  async function readDealFile(name, path) {
    const res = await fetch(`/api/v1/deals/${enc(name)}/files/${path.split('/').map(enc).join('/')}`, { headers: APP.authHeaders() });
    if (!res.ok) throw new Error(`could not be fetched (${res.status})`);
    const blob = await res.blob();
    return APP.readFile(new File([blob], path.split('/').pop(), { type: blob.type }));
  }

  // Opens a deal folder: reads every document, then shows the saved review, or checks the documents if there is
  // none or they changed. Resolves to false when the folder holds no documents.
  async function openDeal(name, options) {
    await flushSave();
    deal.loading = true;
    try {
      return await loadDeal(name, options);
    } finally {
      deal.loading = false;
      if (deal.checked && S.result) scheduleSave();   // a fresh check is saved; a restored review already is
      deal.checked = false;
    }
  }

  async function loadDeal(name, options) {
    const recheck = Boolean(options && options.recheck);
    deal.undo = [];   // "bring it back" only reaches marks of the deal on screen
    deal.name = name;
    deal.list = null;
    $('deal-bar').hidden = false;
    $('deal-name').textContent = name;
    $('deal-saved').textContent = '';
    $('setup').hidden = true;
    history.replaceState(null, '', `?deal=${enc(name)}`);
    document.title = `${name}: Fine Print`;
    await APP.ready;
    status(`Opening ${name}…`);
    const { files } = await api(`/api/v1/deals/${enc(name)}/files`);
    if (!files.length) {
      status(`The folder "${name}" has no documents yet. Put PDF, Word or text files in it${deal.root ? ` (${deal.root}\\${name})` : ''}, then choose Check again.`);
      $('results').hidden = true;
      return false;
    }
    S.docs = [];
    S.ocrRead = [];
    APP.onProgress = status;
    const problems = [];
    for (let i = 0; i < files.length; i += 1) {
      status(`Reading ${i + 1} of ${files.length}: ${files[i].path}`);
      try {
        APP.addDocument(files[i].path, await readDealFile(name, files[i].path));
      } catch (err) {
        problems.push(`${files[i].path}: ${err.message}`);
      }
    }
    const saved = await api(`/api/v1/deals/${enc(name)}/review`).catch(() => ({ saved: false }));
    const review = saved.saved ? saved.review : null;
    const skipped = problems.length ? ` Could not be checked: ${problems.join('; ')}` : '';
    const scans = S.ocrRead.length ? ` Read from a scan with OCR, so check the marks against the page: ${S.ocrRead.join(', ')}.` : '';
    if (review && !recheck && sameFiles(review.documents, fingerprint(S.docs))) {
      restore(review);
      status(`Opened ${name}: ${APP.plural(S.docs.length, 'document', 'documents')}, as you left it.${scans}${skipped}`);
      return true;
    }
    deal.dismissedKeys = review ? review.dismissedKeys || [] : [];
    S.lenses = new Set(FP.LENSES.map((l) => l.id));
    status(`Checking ${APP.plural(S.docs.length, 'document', 'documents')}…`);
    await APP.run();
    deal.checked = true;
    const kept = deal.dismissedKeys.length ? ' Marks you removed before stay removed.' : '';
    status(`${review ? 'The documents changed since the last review, so they were checked again.' : `Checked ${name}.`}${kept}${scans}${skipped}`);
    return true;
  }

  /* ---------------------------------------------------------------- */
  /* export                                                            */
  /* ---------------------------------------------------------------- */

  // The server counts characters as Python does (code points); this page counts UTF-16 units.
  function toCodePoints(text, units) {
    return /[\uD800-\uDFFF]/.test(text) ? Array.from(text.slice(0, units)).length : units;
  }

  // The marked copies of the last export, downloaded to the visitor's computer as one zip.
  async function downloadZip() {
    const res = await fetch(`/api/v1/deals/${enc(deal.name)}/export.zip`, { headers: APP.authHeaders() });
    if (!res.ok) return;
    const link = document.createElement('a');
    link.href = URL.createObjectURL(await res.blob());
    link.download = `${deal.name} - marked documents.zip`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 10000);
  }

  async function exportMarked() {
    if (!deal.name || !S.result) return 'Open a deal folder first: exported copies are saved into its folder.';
    const findings = APP.activeFindings().map((f) => ({
      id: f.id, severity: f.severity, title: f.title, why: f.why, ask: f.ask, detail: f.detail || '',
      spans: f.spans.map((s) => ({
        doc: s.doc, start: toCodePoints(S.analyzed[s.doc].text, s.start), end: toCodePoints(S.analyzed[s.doc].text, s.end),
      })),
    }));
    status('Exporting…');
    try {
      const documents = S.analyzed.map((d) => ({ name: d.name, text: d.text }));
      const out = await api(`/api/v1/deals/${enc(deal.name)}/export`, jsonBody('POST', { documents, findings }));
      $('deal-download').hidden = false;
      await downloadZip();
      const message = `Exported ${APP.plural(out.written.length - 1, 'marked document', 'marked documents')} and a summary: the zip is downloading (open summary.html inside it). They are also in the deal's _marked folder.`;
      status(message);
      return message;
    } catch (err) {
      const message = `The export failed: ${err.message}`;
      status(message);
      return message;
    }
  }

  /* ---------------------------------------------------------------- */
  /* the assistant's actions                                           */
  /* ---------------------------------------------------------------- */

  const ranked = () => [...APP.activeFindings()].sort(bySeverity);
  const firstSentence = (text) => (String(text || '').match(/^.*?[.!?](\s|$)/) || [String(text || '')])[0].trim();
  const describe = (f) => `${APP.sevLabel(f.severity)}: ${f.title}. ${firstSentence(f.why)}`;

  function overview() {
    const active = ranked();
    const verdict = FP.verdictFor(active);
    deal.list = active;
    deal.cursor = -1;
    if (!active.length) return `${verdict.headline}`;
    const top = active.slice(0, 3).map((f, i) => `${i + 1}. ${f.title}`).join(' ');
    const counts = FP.SEVERITIES.filter((s) => verdict.counts[s]).map((s) => `${verdict.counts[s]} ${s}`).join(', ');
    const earnings = S.earnings && S.earnings.line ? ` ${S.earnings.line}` : '';
    return `${verdict.headline} ${APP.plural(active.length, 'problem', 'problems')} (${counts}).${earnings} The most serious: ${top} Say "next" to go through them.`;
  }

  function problemsIn(name) {
    const index = S.analyzed.findIndex((d) => d.name === name);
    if (index < 0) return `I could not find ${name} in this deal.`;
    const list = ranked().filter((f) => FP.documentsOf(f).includes(index));
    if (!list.length) return `No problems are marked in ${name}.`;
    deal.list = list;
    deal.cursor = 0;
    APP.showInDocument(list[0].id, index);
    return `${name} has ${APP.plural(list.length, 'problem', 'problems')}. The most serious is ${describe(list[0])} Say "next" for the next one, or "not a problem" to remove this mark.`;
  }

  function step(direction) {
    const list = (deal.list || ranked()).filter((f) => !S.dismissed.has(f.id));
    if (!list.length) return 'There are no problems to go through.';
    deal.list = list;
    deal.cursor = (deal.cursor + direction + list.length) % list.length;
    const f = list[deal.cursor];
    APP.showInDocument(f.id);
    return `${deal.cursor + 1} of ${list.length}. ${describe(f)} Ask: ${f.ask}`;
  }

  function current() {
    const id = S.noteId || S.selected || (deal.list && deal.list[deal.cursor] ? deal.list[deal.cursor].id : null);
    return id ? S.result.findings.find((f) => f.id === id) : null;
  }

  function dismissCurrent() {
    const f = current();
    if (!f) return 'Select a mark first, or say "next problem".';
    if (!S.dismissed.has(f.id)) APP.toggleDismiss(f.id);
    deal.undo.push(f.id);
    if (deal.list) {
      deal.list = deal.list.filter((x) => x.id !== f.id);
      deal.cursor = Math.min(deal.cursor, deal.list.length) - 1;
    }
    return `Removed the mark "${f.title}". Say "bring it back" to undo.`;
  }

  function restoreLast() {
    const id = deal.undo.pop();
    if (!id) return 'There is no removed mark to bring back.';
    if (S.dismissed.has(id)) APP.toggleDismiss(id);
    const f = S.result.findings.find((x) => x.id === id);
    deal.list = null;
    return `Brought back "${f ? f.title : 'the mark'}".`;
  }

  function confirmCurrent() {
    const f = current();
    if (!f) return 'Select a mark first, or say "next problem".';
    if (!S.confirmed.has(f.id)) APP.toggleConfirm(f.id);
    return `Confirmed "${f.title}" as a real problem.`;
  }

  function showArea(category) {
    if (!S.result) return 'Open a deal folder or add documents first.';
    APP.showArea(category);
    const n = category === 'all' ? APP.activeFindings().length : APP.activeFindings().filter((f) => f.lens === category).length;
    const name = category === 'all' ? '' : `${FP.LENS_NAME[category]} `;
    return `Showing ${APP.plural(n, `${name}problem`, `${name}problems`)}.`;
  }

  function context() {
    const docs = S.analyzed.length ? S.analyzed : S.docs;
    return { deals: deal.names, documents: docs.map((d) => d.name), categories: FP.LENSES.map((l) => ({ id: l.id, name: l.name })) };
  }

  // The problems as the AI fallback sees them: numbered, most serious first, so its answer can cite them.
  function numberedFindings() {
    if (!S.result) return [];
    return ranked().slice(0, 40).map((f, i) => ({
      n: i + 1, id: f.id, title: String(f.title).slice(0, 300), severity: f.severity, why: String(f.why || '').slice(0, 400),
      documents: FP.documentsOf(f).map((d) => S.analyzed[d] && S.analyzed[d].name).filter(Boolean).slice(0, 10),
    }));
  }

  // Wording the phrase matcher does not know goes to Nemotron, which picks a command or answers from the problems.
  async function askModel(text) {
    if (!S.ai.live) return null;
    const ctx = context();
    const findings = numberedFindings();
    const command = await api('/api/v1/assistant/interpret', jsonBody('POST', {
      text: text.slice(0, 500), deals: ctx.deals, documents: ctx.documents, categories: ctx.categories.map((c) => c.id),
      findings: findings.map(({ id, ...rest }) => rest),
    }));
    return { command, findings };
  }

  function answerWith(command, findings) {
    const cited = command.cites.map((n) => findings[n - 1]).filter(Boolean);
    if (cited[0]) APP.showInDocument(cited[0].id);
    return `${command.answer} (Based on: ${cited.map((f) => f.title).join('; ')}.)`;
  }

  const CHANGES = new Set(['dismiss', 'restore', 'confirm', 'export']);
  const CHANGE_WORDS = { dismiss: 'not a problem', restore: 'bring it back', confirm: 'confirm', export: 'export' };

  async function handle(text) {
    let c = AS ? AS.parseCommand(text, context()) : { action: 'unknown' };
    if (c.action === 'unknown') {
      const asked = await askModel(text).catch(() => null);
      if (asked && asked.command.action === 'answer') return answerWith(asked.command, asked.findings);
      if (asked && CHANGES.has(asked.command.action)) {
        // The AI may misread, and document text can try to steer it: it never removes, confirms or exports by itself.
        return `Did you mean "${CHANGE_WORDS[asked.command.action]}"? Say it in those words and I will do it.`;
      }
      if (asked && asked.command.action !== 'unknown') c = asked.command;
      else if (c.guess) c = c.guess;   // no AI answer: fall back to the phrase match
    }
    return dispatch(c, text);
  }

  async function dispatch(c, text) {
    const needResult = 'Open a deal folder or add documents first. Say "list my folders" to hear them.';
    switch (c.action) {
      case 'stop': return '';
      case 'help': return HELP;
      case 'list-deals': {
        await loadDealNames();
        return deal.names.length ? `You have ${APP.plural(deal.names.length, 'folder', 'folders')}: ${deal.names.join(', ')}.` : `There are no deal folders yet. Make one folder per deal in ${deal.root}.`;
      }
      case 'open-deal':
        return (await openDeal(c.deal)) ? `Opened ${c.deal}. ${overview()}` : `${c.deal} has no documents yet.`;
      case 'problems':
        if (c.deal && c.deal !== deal.name && !(await openDeal(c.deal))) return `${c.deal} has no documents yet.`;
        if (!S.result) return needResult;
        return c.document ? problemsIn(c.document) : overview();
      case 'summary': return S.result ? overview() : needResult;
      case 'next': return S.result ? step(1) : needResult;
      case 'previous': return S.result ? step(-1) : needResult;
      case 'dismiss': return S.result ? dismissCurrent() : needResult;
      case 'restore': return restoreLast();
      case 'confirm': return S.result ? confirmCurrent() : needResult;
      case 'area': return showArea(c.category);
      case 'export': return exportMarked();
      default: return `Sorry, I did not understand "${text}". ${HELP}`;
    }
  }

  /* ---------------------------------------------------------------- */
  /* start                                                             */
  /* ---------------------------------------------------------------- */

  async function init() {
    $('deal-export').addEventListener('click', exportMarked);
    $('deal-download').addEventListener('click', downloadZip);
    $('deal-recheck').addEventListener('click', () => openDeal(deal.name, { recheck: true }).catch((err) => status(err.message)));
    await loadDealNames().catch(() => {});
    const VIEW_NAMES = { findings: 'the findings', document: 'the document', followups: 'the questions to send',
      connections: 'the links between documents', workflow: 'the deal tracker' };
    const voice = window.FinePrintVoice ? window.FinePrintVoice.mount({
      handle,
      screen: {
        fullscreen: (on) => APP.setFocus(on),
        zoom: (direction) => APP.setZoom(direction),
        scrollTarget: () => APP.scrollTarget(),
        view: (v) => {
          if (!S.result) return 'Open a deal or check some documents first.';
          APP.setView(v);
          return `Showing ${VIEW_NAMES[v] || v}.`;
        },
      },
    }) : null;
    const params = new URLSearchParams(window.location.search);
    const name = params.get('deal');
    if (name) {
      try {
        await openDeal(name);
      } catch (err) {
        status(`Could not open "${name}": ${err.message}`);
        $('setup').hidden = false;
      }
    }
    const ask = params.get('ask');
    // A question carried over from the home page. A link must not change anything, so only questions run.
    if (ask && voice && AS && !CHANGES.has(AS.parseCommand(ask, context()).action)) voice.ask(ask);
  }

  init();
})();
