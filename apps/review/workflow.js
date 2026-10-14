/*
 * Fine Print workflow layer.
 * Turns the dossier and findings into the deliverables a diligence team works from: an information request list (IRL),
 * a Q&A log, a red-flag treatment per finding, a reviewer sign-off status and coverage by workstream.
 * Pure functions, plain objects in and out, no DOM. Works in the browser (window.FinePrintWorkflow) and in Node.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.FinePrintWorkflow = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // Data-room style index. Based on the common numbered folder structure (1.0 Corporate, 2.0 Financial, 3.0 Legal,
  // 4.0 HR, 5.0 IP and Technology, ... 8.0 Regulatory, 9.0 Real Estate; https://www.papermark.com/blog/data-room-folder-structure),
  // with corporate and legal merged into 1 and IP split from technology. Numbering makes request refs unambiguous.
  const WORKSTREAMS = [
    { id: 'legal', ref: 1, name: 'Corporate and legal' },
    { id: 'financial', ref: 2, name: 'Financial' },
    { id: 'people', ref: 3, name: 'People and HR' },
    { id: 'ip', ref: 4, name: 'Intellectual property' },
    { id: 'it_security', ref: 5, name: 'Technology and IT security' },
    { id: 'commercial', ref: 6, name: 'Commercial and operations' },
    { id: 'tax', ref: 7, name: 'Tax' },
    { id: 'regulatory', ref: 8, name: 'Regulatory and compliance' },
    { id: 'real_estate', ref: 9, name: 'Real estate and assets' },
    { id: 'esg_environmental', ref: 10, name: 'ESG and environmental' },
    { id: 'other', ref: 11, name: 'Other' },
  ];
  const WS = Object.fromEntries(WORKSTREAMS.map((w) => [w.id, w]));

  // dossier.js document kinds -> workstream.
  const KIND_WORKSTREAM = {
    contract: 'legal', financial: 'financial', policy: 'regulatory', 'legal-order': 'regulatory',
    technical: 'it_security', property: 'real_estate', people: 'people', correspondence: 'other', unknown: 'other',
  };
  // engine.js lenses -> workstream.
  const LENS_WORKSTREAM = {
    financial: 'financial', legal: 'legal', compliance: 'regulatory', tech: 'it_security', people: 'people',
    property: 'real_estate', tax: 'tax', commercial: 'commercial', supply: 'commercial', integrity: 'other',
  };
  // Named documents that are referred to but not supplied, where the label itself says which workstream asks for it.
  const LABEL_WORKSTREAM = [
    [/audit|management letter|cap table|bank confirmation|compliance certificate|accounts/i, 'financial'],
    [/side letter|board minutes|master services agreement|term sheet|disclosure schedule/i, 'legal'],
  ];

  const RANK = { critical: 0, high: 1, medium: 2, low: 3 };
  const PRIORITY_RANK = { high: 0, medium: 1, low: 2 };
  const STATUS_LABEL = { missing: 'Outstanding', unclear: 'To confirm' };
  const CITATION_MAX = 300;

  const workstreamOf = (id) => (WS[id] ? id : 'other');
  const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : '');
  const baseId = (id) => String(id || '').replace(/-\d+$/, '');

  /* ---------------------------------------------------------------- */
  /* information request list                                          */
  /* ---------------------------------------------------------------- */

  function referencedWorkstream(gap, dossier) {
    const label = (gap.labels || [gap.item]).join(' ');
    const byLabel = LABEL_WORKSTREAM.find(([re]) => re.test(label));
    if (byLabel) return byLabel[1];
    const kinds = new Set((gap.documents || [])
      .map((i) => (dossier.documents || [])[i])
      .filter(Boolean)
      .map((d) => KIND_WORKSTREAM[d.kind] || 'other'));
    return kinds.size === 1 ? [...kinds][0] : 'other';
  }

  function requestFromGap(gap, dossier) {
    if (gap.type === 'required-by-reasoning') {
      if (gap.status === 'supplied') return null;
      return {
        workstream: workstreamOf(gap.workstream),
        request: gap.whatItIs ? `${gap.item}: ${gap.whatItIs}` : gap.item,
        priority: PRIORITY_RANK[gap.priority] != null ? gap.priority : 'medium',
        status: STATUS_LABEL[gap.status] || 'Outstanding',
        why: gap.why || '',
        source: 'Worked out by the reasoning model',
      };
    }
    if (gap.type === 'referenced-not-supplied') {
      return {
        workstream: referencedWorkstream(gap, dossier),
        request: gap.item,
        priority: 'medium',
        status: 'Outstanding',
        why: gap.why || '',
        source: 'Referred to in the documents',
      };
    }
    return null;
  }

  function numberByWorkstream(items) {
    const order = (x) => WS[x.workstream].ref * 10 + PRIORITY_RANK[x.priority];
    const sorted = items.map((x, i) => ({ x, i })).sort((a, b) => order(a.x) - order(b.x) || a.i - b.i);
    const seen = {};
    return sorted.map(({ x }) => {
      seen[x.workstream] = (seen[x.workstream] || 0) + 1;
      return Object.assign({ ref: `${WS[x.workstream].ref}.${seen[x.workstream]}` }, x);
    });
  }

  /**
   * Request list rows from the dossier gaps. `findings` is accepted for API symmetry; requests come from gaps only.
   * IRLs are grouped by workstream with status and priority (hyperbots.com/glossary/due-diligence-request-list).
   * @param {object} dossier result of FinePrintDossier.build
   * @param {Array} findings unused
   * @param {string} today ISO date stamped as `raised`
   */
  function irlRows(dossier, findings, today) {
    const gaps = (dossier && dossier.gaps) || [];
    const items = gaps.map((g) => requestFromGap(g, dossier)).filter(Boolean);
    return numberByWorkstream(items).map((r) => ({
      ref: r.ref,
      key: requestKey(r.workstream, r.request),
      workstream: WS[r.workstream].name,
      workstreamId: r.workstream,
      request: r.request,
      priority: cap(r.priority),
      status: r.status,
      why: r.why,
      source: r.source,
      raised: today || '',
    }));
  }

  /* ---------------------------------------------------------------- */
  /* reviewer status on requests                                       */
  /* ---------------------------------------------------------------- */

  const REQUEST_STATUSES = ['Outstanding', 'To confirm', 'Received', 'Not applicable'];
  // A resolved request needs a reason or a location, so the next reader can check it.
  const RESOLVED_STATUSES = new Set(['Received', 'Not applicable']);
  const NOTE_MAX = 300;

  const isResolved = (r) => Boolean(r && (r.resolved || RESOLVED_STATUSES.has(r.status)));
  const cleanNote = (s) => String(s == null ? '' : s).replace(/\s+/g, ' ').trim().slice(0, NOTE_MAX);
  const asMap = (x) => (x instanceof Map ? x : new Map(Object.entries(x || {})));

  /**
   * Stable key for a request: the same ask in the same workstream keeps its key when refs are renumbered.
   * @param {string} workstreamId
   * @param {string} request
   */
  function requestKey(workstreamId, request) {
    return `${workstreamOf(workstreamId)}|${String(request || '').toLowerCase().replace(/\s+/g, ' ').trim()}`;
  }

  function reviewedRow(row, override) {
    const status = REQUEST_STATUSES.includes(override && override.status) ? override.status : null;
    const note = cleanNote(override && override.note);
    const needsNote = status && RESOLVED_STATUSES.has(status) && !note;
    const applied = status && !needsNote ? status : null;
    return Object.assign({}, row, {
      status: applied || row.status,
      note,
      statusBy: applied ? 'Reviewer' : 'Fine Print',
      resolved: Boolean(applied && RESOLVED_STATUSES.has(applied)),
      pending: needsNote ? status : '',
    });
  }

  /**
   * Apply the reviewer's status and note to request rows. Received and Not applicable only take effect with a note;
   * until then the row keeps its status and `pending` names the status waiting for a note. Rows are never mutated.
   * @param {Array} rows irlRows output
   * @param {Map|object} overrides request key -> {status, note}
   */
  function applyRequestStatus(rows, overrides) {
    const map = asMap(overrides);
    return (rows || []).map((r) => reviewedRow(r, map.get(r.key || requestKey(r.workstreamId, r.request))));
  }

  /* ---------------------------------------------------------------- */
  /* CSV (RFC 4180)                                                     */
  /* ---------------------------------------------------------------- */

  const IRL_COLUMNS = [['ref', 'Ref'], ['workstream', 'Workstream'], ['request', 'Request'], ['priority', 'Priority'],
    ['status', 'Status'], ['statusBy', 'Status set by'], ['note', 'Reviewer note'], ['why', 'Why'], ['source', 'Source'], ['raised', 'Raised']];
  const QA_COLUMNS = [['ref', 'Ref'], ['workstream', 'Workstream'], ['question', 'Question'], ['because', 'Because'],
    ['citation', 'Citation'], ['severity', 'Severity'], ['status', 'Status'], ['memo', 'Memo ref']];

  function csvField(value) {
    let s = value == null ? '' : String(value);
    // Spreadsheet formula injection guard: document text must never execute as a formula when the CSV is opened.
    if (/^[=+\-@\t\r]/.test(s)) s = `'${s}`;
    return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  }

  /**
   * @param {Array<object>} rows
   * @param {Array<[string,string]>} [columns] [key, header] pairs; defaults to the keys of the first row
   */
  function toCsv(rows, columns) {
    const cols = columns || Object.keys((rows && rows[0]) || {}).map((k) => [k, k]);
    const lines = [cols.map(([, header]) => csvField(header)).join(',')];
    for (const row of rows || []) lines.push(cols.map(([key]) => csvField(row[key])).join(','));
    return `${lines.join('\r\n')}\r\n`;
  }

  /* ---------------------------------------------------------------- */
  /* Q&A log                                                           */
  /* ---------------------------------------------------------------- */

  function citationOf(f) {
    const text = f.quote && f.quote.text ? f.quote.text.replace(/\s+/g, ' ').trim() : '';
    if (!text) return f.places || '';
    const cut = text.length > CITATION_MAX ? `${text.slice(0, CITATION_MAX - 1)}…` : text;
    return f.places ? `"${cut}" (${f.places})` : `"${cut}"`;
  }

  /**
   * One question per distinct ask, highest severity first.
   * @param {Array} findings
   * @param {{dismissed?: Set|Array, memoRefs?: Array<string>}} [options]
   */
  function qaRows(findings, options) {
    const opts = options || {};
    const dismissed = new Set(opts.dismissed || []);
    const memoRefs = opts.memoRefs || [];
    const live = (findings || []).filter((f) => !dismissed.has(f.id) && f.ask)
      .map((f, i) => ({ f, i }))
      .sort((a, b) => (RANK[a.f.severity] ?? 9) - (RANK[b.f.severity] ?? 9) || a.i - b.i)
      .map(({ f }) => f);
    const byAsk = new Map();
    for (const f of live) {
      const key = f.ask.trim();
      if (!byAsk.has(key)) byAsk.set(key, { first: f, titles: [], memo: [] });
      const entry = byAsk.get(key);
      if (!entry.titles.includes(f.title)) entry.titles.push(f.title);
      const m = memoRefs.indexOf(f.id);
      if (m >= 0) entry.memo.push(`F${m + 1}`);
    }
    return [...byAsk.values()].map((e, i) => ({
      ref: `Q${i + 1}`,
      workstream: WS[LENS_WORKSTREAM[e.first.lens] || 'other'].name,
      question: e.first.ask.trim(),
      because: e.titles.join('; '),
      citation: citationOf(e.first),
      severity: cap(e.first.severity),
      status: 'Draft',
      memo: e.memo.join(', '),
    }));
  }

  /* ---------------------------------------------------------------- */
  /* suggested treatment for a red flag                                */
  /* ---------------------------------------------------------------- */

  // Treatments from Paul Hastings, "Legal due diligence: what really matters":
  // known cost -> price; real but uncertain -> specific indemnity; fixable pre-closing threat -> condition precedent;
  // covenant-strength concern -> escrow/holdback; minor -> post-completion covenant; walk away as a last resort.
  const REMEDY_SOURCE = 'https://www.paulhastings.com/insights/the-buyers-playbook-legal-insights-into-m-and-a/legal-due-diligence-what-really-matters';
  const REMEDY = {
    price: 'Price adjustment',
    indemnity: 'Specific indemnity',
    cp: 'Condition precedent',
    escrow: 'Escrow or holdback',
    walk: 'Escrow or holdback; consider walking away',
    covenant: 'Post-completion covenant',
    info: 'Further information request',
  };

  // Order matters: the first rule that matches wins. ids are engine rule ids (trailing -N stripped).
  const REMEDY_RULES = [
    { key: 'walk', ids: ['fin-going-concern'], words: /going[- ]concern/i,
      basis: 'The business may not survive the next 12 months; protect the price with retained funds, or reconsider the deal.' },
    { key: 'price', ids: ['ppl-cic', 'ppl-pension'], words: null,
      basis: 'A payment the deal itself triggers or a funded obligation is a known cost that can be priced in.' },
    { key: 'cp', ids: ['leg-coc', 'leg-assignment', 'fin-covenant', 'prop-title', 'cmp-licence', 'prop-zoning'],
      words: /change[- ]of[- ]control|change in control|assignment|consent|waiver|licen[cs]e|permit/i,
      basis: 'A threat to completion that can be fixed before closing, such as a consent, waiver, permit or released lien.' },
    { key: 'info', ids: ['int-withheld', 'int-placeholder', 'int-blank-sign', 'int-draft', 'int-undated', 'fin-unaudited', 'leg-missing', 'leg-oral', 'int-unverified'],
      words: /withheld|redact|missing|not supplied|unaudited|draft|unverified|undocumented/i,
      basis: 'The issue is a gap in what was supplied; ask for the material before choosing a treatment.' },
    { key: 'indemnity',
      ids: ['leg-unlimited', 'leg-litigation', 'leg-indemnity', 'leg-guarantee', 'leg-ip', 'tax-dispute', 'tax-tp', 'tax-position', 'tax-indirect',
        'cmp-action', 'cmp-noncompliance', 'cmp-privacy', 'cmp-whistle', 'cmp-financial-crime', 'cmp-product', 'prop-env', 'sup-emissions',
        'sup-labour', 'tech-incident', 'tech-copyleft', 'ppl-claims', 'ppl-classification'],
      words: /uncapped|unlimited|litigation|dispute|claim|lawsuit|tax|regulat|penalt|enforcement|environment|contamination|breach|incident|investigation/i,
      basis: 'A real exposure whose size is uncertain; ring-fence it with a specific indemnity.' },
    { key: 'price',
      ids: ['fin-adjusted', 'fin-oneoff', 'fin-restatement', 'fin-revrec', 'fin-receivables', 'fin-capitalised', 'fin-contingent', 'fin-related',
        'int-footing', 'int-conflict', 'com-pipeline', 'com-lost', 'fin-benford'],
      words: /add[- ]?back|adjusted|one[- ]?off|non[- ]?recurring|restat|revenue|receivable|capitali[sz]|ebitda|working capital/i,
      basis: 'Affects the earnings or balance-sheet figures the price rests on; quantify and adjust the price.' },
    { key: 'escrow', ids: ['fin-liquidity', 'fin-weakness'], words: /liquidity|cash|control weakness/i,
      basis: 'Doubt about recovering a later claim; keep part of the price back in escrow or as a holdback.' },
  ];

  const MINOR = { key: 'covenant', basis: 'Minor; can be put right after completion under a covenant.' };
  const byId = (f) => REMEDY_RULES.find((r) => r.ids.includes(baseId(f.id)));
  const byWords = (f) => REMEDY_RULES.find((r) => r.words && r.words.test(f.title || ''));

  /**
   * Suggested treatment for one finding. A screening suggestion, not legal advice.
   * Known engine rule ids decide first, then low severity, then keywords in the title (AI and web findings).
   * @returns {{remedy: string, basis: string, key: string}}
   */
  function suggestRemedy(finding) {
    const f = finding || {};
    const rule = byId(f) || (f.severity === 'low' ? MINOR : null) || byWords(f);
    if (rule) return { key: rule.key, remedy: REMEDY[rule.key], basis: rule.basis };
    if (f.severity === 'critical' || f.severity === 'high') {
      return { key: 'indemnity', remedy: REMEDY.indemnity, basis: 'A serious issue of uncertain size; ring-fence it until it can be quantified.' };
    }
    return { key: 'info', remedy: REMEDY.info, basis: 'Confirm the scale of the issue before choosing a treatment.' };
  }

  const TREATMENT_OTHER = 'other';

  /**
   * The reviewer's own treatment, if one is validly set: a REMEDY key, or "other" with a description.
   * @param {{key?: string, text?: string}} [override]
   * @returns {{key: string, remedy: string}|null}
   */
  function reviewerTreatment(override) {
    const key = override && override.key;
    if (key === TREATMENT_OTHER) {
      const text = cleanNote(override.text);
      return text ? { key, remedy: text } : null;
    }
    return REMEDY[key] ? { key, remedy: REMEDY[key] } : null;
  }

  /**
   * Suggested treatment plus the reviewer's choice. The suggestion is kept so the report shows both.
   * @returns {{suggested: object, reviewer: object|null, lines: Array<string>}}
   */
  function treatmentFor(finding, override) {
    const suggested = suggestRemedy(finding);
    const reviewer = reviewerTreatment(override);
    const lines = [`Suggested: ${suggested.remedy} (${suggested.basis})`];
    if (reviewer) lines.push(`Reviewer: ${reviewer.remedy}`);
    return { suggested, reviewer, lines };
  }

  /* ---------------------------------------------------------------- */
  /* reviewer sign-off                                                 */
  /* ---------------------------------------------------------------- */

  /**
   * Red flag -> draft -> final: every serious flag is confirmed by a named person before the report is final
   * (https://www.harvey.ai/blog/red-flag-due-diligence).
   * @param {{findings: Array, dismissed?: Set|Array, confirmed?: Set|Array, reviewer?: string, date?: string}} input
   */
  function reportStatus(input) {
    const dismissed = new Set(input.dismissed || []);
    const confirmed = new Set(input.confirmed || []);
    const reviewer = String(input.reviewer || '').trim();
    const serious = (input.findings || []).filter((f) => !dismissed.has(f.id) && (f.severity === 'critical' || f.severity === 'high'));
    const done = serious.filter((f) => confirmed.has(f.id)).length;
    // A final report comes once open issues are resolved (RSM), so high-priority requests still outstanding keep it a draft.
    // Received and Not applicable (each with a reviewer note) count as resolved.
    const requests = input.requests || [];
    const open = requests.filter((r) => !isResolved(r));
    const openHigh = open.filter((r) => r.status === 'Outstanding' && String(r.priority).toLowerCase() === 'high').length;
    const resolved = requests.length - open.length;
    const final = Boolean(reviewer) && done === serious.length && openHigh === 0;
    const parts = [`${done} of ${serious.length} flags confirmed`];
    if (!reviewer) parts.push('no reviewer named');
    if (openHigh) parts.push(`${openHigh} high-priority request${openHigh === 1 ? '' : 's'} still outstanding`);
    const label = final
      ? `FINAL: confirmed by ${reviewer} on ${input.date || ''}`.trim()
      : `DRAFT: ${parts.join('; ')}`;
    return { final, label, confirmed: done, required: serious.length, reviewer, openHigh, resolved };
  }

  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

  /**
   * What the reviewer did, for the end of the report. Counts only actions on findings and requests of this run.
   * @param {{findings: Array, dismissed?: Set|Array, confirmed?: Set|Array, treatments?: Map|object,
   *   requests?: Array, reviewer?: string, date?: string}} input requests are applyRequestStatus output
   */
  function auditTrail(input) {
    const findings = input.findings || [];
    const dismissed = new Set(input.dismissed || []);
    const confirmed = new Set(input.confirmed || []);
    const treatments = asMap(input.treatments);
    const live = findings.filter((f) => !dismissed.has(f.id));
    const requests = input.requests || [];
    const counts = {
      confirmed: live.filter((f) => confirmed.has(f.id)).length,
      dismissed: findings.filter((f) => dismissed.has(f.id)).length,
      treatments: live.filter((f) => reviewerTreatment(treatments.get(f.id))).length,
      resolved: requests.filter((r) => r.statusBy === 'Reviewer' && r.resolved).length,
      restatused: requests.filter((r) => r.statusBy === 'Reviewer' && !r.resolved).length,
    };
    const reviewer = String(input.reviewer || '').trim();
    const lines = [
      `Reviewer: ${reviewer || 'not named'}`,
      `Date: ${input.date || ''}`.trim(),
      `Confirmed ${plural(counts.confirmed, 'flag', 'flags')}`,
      `Dismissed ${plural(counts.dismissed, 'finding', 'findings')}`,
      `Set the treatment on ${plural(counts.treatments, 'red flag', 'red flags')} (replacing the suggestion)`,
      `Resolved ${plural(counts.resolved, 'request', 'requests')} (Received or Not applicable, each with a note)`,
    ];
    if (counts.restatused) lines.push(`Changed the status of ${plural(counts.restatused, 'other request', 'other requests')}`);
    return { counts, reviewer, lines };
  }

  /* ---------------------------------------------------------------- */
  /* coverage by workstream                                            */
  /* ---------------------------------------------------------------- */

  /**
   * Documents supplied, findings and outstanding requests per workstream. Data-room tools report activity;
   * this reports what each workstream actually has to go on.
   * @param {object} dossier
   * @param {Array} findings active (not dismissed) findings
   * @param {Array} [requests] irlRows output; computed when omitted
   */
  function coverage(dossier, findings, requests) {
    const rows = Object.fromEntries(WORKSTREAMS.map((w) => [w.id, { id: w.id, ref: w.ref, workstream: w.name, documents: 0, findings: 0, outstanding: 0 }]));
    for (const d of (dossier && dossier.documents) || []) rows[KIND_WORKSTREAM[d.kind] || 'other'].documents += 1;
    for (const f of findings || []) rows[LENS_WORKSTREAM[f.lens] || 'other'].findings += 1;
    const open = (requests || irlRows(dossier, findings, '')).filter((r) => !isResolved(r));
    for (const r of open) rows[workstreamOf(r.workstreamId)].outstanding += 1;
    return WORKSTREAMS.map((w) => rows[w.id]);
  }

  return {
    WORKSTREAMS, KIND_WORKSTREAM, LENS_WORKSTREAM, IRL_COLUMNS, QA_COLUMNS, REMEDY, REMEDY_SOURCE,
    REQUEST_STATUSES, RESOLVED_STATUSES: [...RESOLVED_STATUSES], TREATMENT_OTHER,
    irlRows, toCsv, qaRows, suggestRemedy, reportStatus, coverage,
    requestKey, applyRequestStatus, treatmentFor, auditTrail,
  };
});
