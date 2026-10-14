// Run with: node --test apps/review/workflow.test.js
const test = require('node:test');
const assert = require('node:assert/strict');
const W = require('./workflow.js');
const D = require('./dossier.js');
const FP = require('./engine.js');

const TODAY = '2026-10-13';
const reqGap = (item, extra) => Object.assign({ type: 'required-by-reasoning', item, whatItIs: '', why: 'needed', priority: 'high', status: 'missing' }, extra);
const refGap = (item, extra) => Object.assign({ type: 'referenced-not-supplied', item, labels: [item], why: 'Referred to in a.txt', documents: [0] }, extra);
const dossierOf = (gaps, kinds) => ({ gaps, documents: (kinds || ['unknown']).map((kind, index) => ({ index, kind })) });
const finding = (id, extra) => Object.assign({ id, lens: 'legal', severity: 'high', title: 't', why: 'w', ask: `ask ${id}`, quote: { text: 'q' }, places: 'a.txt' }, extra);

/* ---- information request list ---- */

test('request list numbers rows by workstream in data-room order with high priority first', () => {
  const d = dossierOf([
    reqGap('Board minutes', { workstream: 'legal', priority: 'medium' }),
    reqGap('Quality of earnings report', { workstream: 'financial', priority: 'medium' }),
    reqGap('Audited accounts', { workstream: 'financial', priority: 'high' }),
    reqGap('Cap table', { workstream: 'legal', priority: 'high' }),
  ]);
  const rows = W.irlRows(d, [], TODAY);
  assert.deepEqual(rows.map((r) => [r.ref, r.request]), [
    ['1.1', 'Cap table'], ['1.2', 'Board minutes'], ['2.1', 'Audited accounts'], ['2.2', 'Quality of earnings report'],
  ]);
  assert.equal(rows[2].workstream, 'Financial');
  assert.equal(rows[0].raised, TODAY);
});

test('model status maps to Outstanding or To confirm, and supplied items are not requested', () => {
  const d = dossierOf([
    reqGap('A', { status: 'missing' }), reqGap('B', { status: 'unclear' }), reqGap('C', { status: 'supplied' }),
  ]);
  const rows = W.irlRows(d, [], TODAY);
  assert.deepEqual(rows.map((r) => [r.request, r.status]), [['A', 'Outstanding'], ['B', 'To confirm']]);
});

test('a missing or unknown workstream falls back to other', () => {
  const rows = W.irlRows(dossierOf([reqGap('A'), reqGap('B', { workstream: 'astrology' })]), [], TODAY);
  assert.ok(rows.every((r) => r.workstreamId === 'other' && r.ref.startsWith('11.')));
});

test('a described item keeps its description in the request text', () => {
  const rows = W.irlRows(dossierOf([reqGap('Customer schedule', { whatItIs: 'revenue by customer' })]), [], TODAY);
  assert.equal(rows[0].request, 'Customer schedule: revenue by customer');
});

test('referenced documents are medium priority, take the workstream of the referring document, or of their label', () => {
  const d = dossierOf([refGap('Schedule 4'), refGap('management letter', { documents: [1] })], ['contract', 'unknown']);
  const rows = W.irlRows(d, [], TODAY);
  const byItem = Object.fromEntries(rows.map((r) => [r.request, r]));
  assert.equal(byItem['Schedule 4'].workstreamId, 'legal');
  assert.equal(byItem['Schedule 4'].priority, 'Medium');
  assert.equal(byItem['management letter'].workstreamId, 'financial');
});

test('single-source gaps are not requests', () => {
  assert.deepEqual(W.irlRows(dossierOf([{ type: 'single-source', item: 'x' }]), [], TODAY), []);
});

test('request list works on a real dossier', () => {
  const docs = [{ name: 'c.txt', text: 'This Agreement between the parties. Termination is governed by Schedule 4. Governing law applies. The party shall indemnify.' }];
  const rows = W.irlRows(D.build(docs, []), [], TODAY);
  assert.ok(rows.some((r) => r.request === 'Schedule 4' && r.workstreamId === 'legal'));
});

/* ---- CSV ---- */

test('CSV quotes fields with commas, quotes or newlines and doubles inner quotes', () => {
  const csv = W.toCsv([{ a: 'plain', b: 'x, y', c: 'say "hi"', d: 'two\nlines' }]);
  assert.equal(csv, 'a,b,c,d\r\nplain,"x, y","say ""hi""","two\nlines"\r\n');
});

test('CSV uses the given column headers and order', () => {
  const csv = W.toCsv([{ ref: '2.1', request: 'Accounts' }], [['request', 'Request'], ['ref', 'Ref']]);
  assert.equal(csv, 'Request,Ref\r\nAccounts,2.1\r\n');
});

test('CSV neutralises text that a spreadsheet would run as a formula', () => {
  const csv = W.toCsv([{ a: '=HYPERLINK("x")', b: '@SUM(1)' }]);
  assert.equal(csv.split('\r\n')[1], `"'=HYPERLINK(""x"")",'@SUM(1)`);
});

test('CSV of no rows is just the header', () => {
  assert.equal(W.toCsv([], W.IRL_COLUMNS), 'Ref,Workstream,Request,Priority,Status,Status set by,Reviewer note,Why,Source,Raised\r\n');
});

/* ---- Q&A log ---- */

test('Q&A log orders by severity, skips dismissed findings and merges identical questions', () => {
  const findings = [
    finding('a', { severity: 'medium', ask: 'Same?', title: 'First' }),
    finding('b', { severity: 'critical', ask: 'Urgent?', title: 'Big' }),
    finding('c', { severity: 'high', ask: 'Same?', title: 'Second' }),
    finding('d', { severity: 'critical', ask: 'Gone?' }),
  ];
  const rows = W.qaRows(findings, { dismissed: new Set(['d']) });
  assert.deepEqual(rows.map((r) => [r.ref, r.question, r.severity]), [['Q1', 'Urgent?', 'Critical'], ['Q2', 'Same?', 'High']]);
  assert.equal(rows[1].because, 'Second; First');
  assert.equal(rows[0].status, 'Draft');
});

test('Q&A citation is trimmed to 300 characters and names the document', () => {
  const rows = W.qaRows([finding('a', { quote: { text: 'x'.repeat(500) }, places: 'deal.pdf' })]);
  const quoted = rows[0].citation.match(/^"(.*)" \(deal\.pdf\)$/);
  assert.ok(quoted);
  assert.equal(quoted[1].length, 300);
});

test('Q&A workstream comes from the finding lens', () => {
  const rows = W.qaRows([finding('a', { lens: 'compliance' }), finding('b', { lens: 'tech' }), finding('c', { lens: 'integrity' })]);
  assert.deepEqual(rows.map((r) => r.workstream), ['Regulatory and compliance', 'Technology and IT security', 'Other']);
});

test('Q&A rows note which memo references they back', () => {
  const rows = W.qaRows([finding('a'), finding('b')], { memoRefs: ['b'] });
  assert.equal(rows[1].memo, 'F1');
  assert.equal(rows[0].memo, '');
});

/* ---- suggested treatment ---- */

const remedyOf = (id, extra) => W.suggestRemedy(finding(id, extra)).key;

test('change of control and assignment consent become conditions precedent', () => {
  assert.equal(remedyOf('leg-coc'), 'cp');
  assert.equal(remedyOf('leg-assignment', { severity: 'medium' }), 'cp');
  assert.equal(remedyOf('ai-1', { title: 'Supplier consent needed on change of control' }), 'cp');
});

test('uncapped liability, litigation, tax disputes, regulator action and contamination become specific indemnities', () => {
  for (const id of ['leg-unlimited', 'leg-litigation', 'tax-dispute', 'cmp-action', 'prop-env']) assert.equal(remedyOf(id), 'indemnity', id);
  assert.equal(remedyOf('tech-copyleft', { title: 'Open-source licence risk' }), 'indemnity');
});

test('price-related financial findings become price adjustments', () => {
  for (const id of ['fin-adjusted', 'fin-oneoff', 'fin-restatement', 'int-footing-2', 'ppl-cic']) assert.equal(remedyOf(id), 'price', id);
  assert.equal(remedyOf('ai-2', { title: 'Add-backs not supported by invoices' }), 'price');
});

test('going-concern doubt points to escrow or walking away', () => {
  const r = W.suggestRemedy(finding('fin-going-concern', { severity: 'critical' }));
  assert.equal(r.key, 'walk');
  assert.match(r.remedy, /escrow/i);
  assert.match(r.remedy, /walking away/i);
});

test('withheld or missing information becomes a further information request', () => {
  assert.equal(remedyOf('int-withheld'), 'info');
  assert.equal(remedyOf('leg-missing-2'), 'info');
  assert.equal(remedyOf('ai-3', { title: 'Schedule redacted', severity: 'medium' }), 'info');
});

test('low-severity findings without a specific treatment become post-completion covenants', () => {
  assert.equal(remedyOf('int-absolute', { severity: 'low', title: 'Absolute claims' }), 'covenant');
});

test('liquidity pressure suggests escrow or holdback, and unknown serious findings default to indemnity', () => {
  assert.equal(remedyOf('fin-liquidity'), 'escrow');
  assert.equal(remedyOf('xyz', { title: 'Something odd' }), 'indemnity');
  assert.equal(remedyOf('xyz', { title: 'Something odd', severity: 'medium' }), 'info');
});

test('every treatment has a basis and the source is the Paul Hastings article', () => {
  const r = W.suggestRemedy(finding('leg-coc'));
  assert.ok(r.basis.length > 10);
  assert.match(W.REMEDY_SOURCE, /^https:\/\/www\.paulhastings\.com\//);
});

test('every engine rule id maps to a treatment', () => {
  const r = FP.analyze([{ name: 'x', text: 'change of control. unlimited liability. going concern. restated. tax audit. contamination.' }], { today: TODAY });
  assert.ok(r.findings.length >= 5);
  for (const f of r.findings) assert.ok(W.REMEDY[W.suggestRemedy(f).key]);
});

/* ---- reviewer sign-off ---- */

const signoffFindings = [finding('a', { severity: 'critical' }), finding('b', { severity: 'high' }), finding('c', { severity: 'medium' })];

test('report stays draft until every serious flag is confirmed', () => {
  const s = W.reportStatus({ findings: signoffFindings, confirmed: new Set(['a']), reviewer: 'J. Smith', date: TODAY });
  assert.equal(s.final, false);
  assert.equal(s.label, 'DRAFT: 1 of 2 flags confirmed');
});

test('report needs a named reviewer even when all flags are confirmed', () => {
  const s = W.reportStatus({ findings: signoffFindings, confirmed: new Set(['a', 'b']), reviewer: '  ', date: TODAY });
  assert.equal(s.final, false);
  assert.match(s.label, /^DRAFT: 2 of 2 flags confirmed; no reviewer named$/);
});

test('report is final once a named reviewer confirms every serious flag; dismissed flags are not required', () => {
  const s = W.reportStatus({ findings: signoffFindings, dismissed: new Set(['b']), confirmed: new Set(['a']), reviewer: 'J. Smith', date: TODAY });
  assert.equal(s.final, true);
  assert.equal(s.label, `FINAL: confirmed by J. Smith on ${TODAY}`);
});

/* ---- coverage ---- */

test('coverage counts documents, findings and outstanding requests per workstream', () => {
  const d = dossierOf([reqGap('QoE', { workstream: 'financial' }), refGap('Schedule 4')], ['contract', 'financial', 'policy']);
  const findings = [finding('a', { lens: 'legal' }), finding('b', { lens: 'compliance' }), finding('c', { lens: 'supply' }), finding('d', { lens: 'integrity' })];
  const rows = Object.fromEntries(W.coverage(d, findings).map((r) => [r.id, r]));
  assert.deepEqual([rows.legal.documents, rows.legal.findings, rows.legal.outstanding], [1, 1, 1]);
  assert.deepEqual([rows.financial.documents, rows.financial.outstanding], [1, 1]);
  assert.deepEqual([rows.regulatory.documents, rows.regulatory.findings], [1, 1]);
  assert.equal(rows.commercial.findings, 1);
  assert.equal(rows.other.findings, 1);
  assert.equal(W.coverage(d, findings).length, W.WORKSTREAMS.length);
});

test('a report stays a draft while high-priority requests are outstanding, and says so', () => {
  const findings = [{ id: 'a', severity: 'high' }];
  const requests = [{ status: 'Outstanding', priority: 'High' }, { status: 'Outstanding', priority: 'Medium' }];
  const s = W.reportStatus({ findings, confirmed: new Set(['a']), reviewer: 'J. Smith', date: TODAY, requests });
  assert.equal(s.final, false);
  assert.equal(s.openHigh, 1);
  assert.match(s.label, /^DRAFT: 1 of 1 flags confirmed; 1 high-priority request still outstanding$/);
  const done = W.reportStatus({ findings, confirmed: new Set(['a']), reviewer: 'J. Smith', date: TODAY, requests: requests.slice(1) });
  assert.equal(done.final, true);
});

/* ---- reviewer status on requests ---- */

const twoRequests = () => W.irlRows(dossierOf([
  reqGap('Audited accounts', { workstream: 'financial', priority: 'high' }),
  reqGap('Board minutes', { workstream: 'legal', priority: 'medium' }),
]), [], TODAY);

test('request keys are stable across renumbering and ignore case and spacing', () => {
  const rows = twoRequests();
  assert.equal(rows.find((r) => r.request === 'Audited accounts').key, 'financial|audited accounts');
  assert.equal(W.requestKey('financial', '  Audited   ACCOUNTS '), 'financial|audited accounts');
  assert.equal(W.requestKey('astrology', 'x'), 'other|x');
});

test('applyRequestStatus sets status and note by key without mutating the rows', () => {
  const rows = twoRequests();
  const before = JSON.stringify(rows);
  const out = W.applyRequestStatus(rows, new Map([['financial|audited accounts', { status: 'Received', note: ' Data room 2.4 ' }]]));
  const acc = out.find((r) => r.request === 'Audited accounts');
  assert.deepEqual([acc.status, acc.note, acc.statusBy, acc.resolved, acc.pending], ['Received', 'Data room 2.4', 'Reviewer', true, '']);
  const minutes = out.find((r) => r.request === 'Board minutes');
  assert.deepEqual([minutes.status, minutes.statusBy, minutes.resolved], ['Outstanding', 'Fine Print', false]);
  assert.equal(JSON.stringify(rows), before);
});

test('Received and Not applicable need a note; without one the row stays open and shows what is pending', () => {
  const out = W.applyRequestStatus(twoRequests(), { 'legal|board minutes': { status: 'Not applicable', note: '   ' } });
  const minutes = out.find((r) => r.request === 'Board minutes');
  assert.deepEqual([minutes.status, minutes.resolved, minutes.pending, minutes.statusBy], ['Outstanding', false, 'Not applicable', 'Fine Print']);
});

test('To confirm needs no note, and unknown statuses are ignored', () => {
  const out = W.applyRequestStatus(twoRequests(), {
    'legal|board minutes': { status: 'To confirm' },
    'financial|audited accounts': { status: 'Done', note: 'x' },
  });
  assert.deepEqual(out.map((r) => [r.request, r.status, r.statusBy]),
    [['Board minutes', 'To confirm', 'Reviewer'], ['Audited accounts', 'Outstanding', 'Fine Print']]);
});

test('a resolved high-priority request no longer keeps the report a draft, and is counted', () => {
  const findings = [{ id: 'a', severity: 'high' }];
  const requests = W.applyRequestStatus(twoRequests(), { 'financial|audited accounts': { status: 'Not applicable', note: 'Company is exempt from audit' } });
  const s = W.reportStatus({ findings, confirmed: ['a'], reviewer: 'J. Smith', date: TODAY, requests });
  assert.equal(s.final, true);
  assert.equal(s.resolved, 1);
  const open = W.reportStatus({ findings, confirmed: ['a'], reviewer: 'J. Smith', date: TODAY, requests: twoRequests() });
  assert.equal(open.final, false);
});

test('coverage does not count resolved requests as open', () => {
  const d = dossierOf([reqGap('QoE', { workstream: 'financial' })], ['financial']);
  const requests = W.applyRequestStatus(W.irlRows(d, [], TODAY), { 'financial|qoe': { status: 'Received', note: 'email 3 Oct' } });
  assert.equal(W.coverage(d, [], requests).find((r) => r.id === 'financial').outstanding, 0);
});

test('CSV of reviewed requests carries the status, who set it and the note', () => {
  const out = W.applyRequestStatus(twoRequests(), { 'financial|audited accounts': { status: 'Received', note: 'VDR 2.4' } });
  const line = W.toCsv(out, W.IRL_COLUMNS).split('\r\n').find((l) => l.includes('Audited accounts'));
  assert.match(line, /,Received,Reviewer,VDR 2\.4,/);
});

/* ---- reviewer treatment ---- */

test('treatmentFor keeps the suggestion and adds the reviewer choice when one is set', () => {
  const f = finding('leg-coc');
  const plain = W.treatmentFor(f);
  assert.equal(plain.reviewer, null);
  assert.deepEqual(plain.lines, [`Suggested: ${W.REMEDY.cp} (${plain.suggested.basis})`]);
  const set = W.treatmentFor(f, { key: 'price' });
  assert.deepEqual(set.reviewer, { key: 'price', remedy: W.REMEDY.price });
  assert.equal(set.lines[1], `Reviewer: ${W.REMEDY.price}`);
  assert.equal(set.suggested.key, 'cp');
});

test('an "other" treatment needs a description, and unknown keys are ignored', () => {
  const f = finding('leg-coc');
  assert.equal(W.treatmentFor(f, { key: W.TREATMENT_OTHER, text: '  ' }).reviewer, null);
  assert.equal(W.treatmentFor(f, { key: 'bogus' }).reviewer, null);
  const other = W.treatmentFor(f, { key: W.TREATMENT_OTHER, text: 'Seller to obtain\nconsent pre-signing' });
  assert.deepEqual(other.reviewer, { key: 'other', remedy: 'Seller to obtain consent pre-signing' });
});

/* ---- audit trail ---- */

test('audit trail counts what the reviewer did and names them', () => {
  const findings = [finding('a', { severity: 'critical' }), finding('b'), finding('c', { severity: 'medium' })];
  const requests = W.applyRequestStatus(twoRequests(), {
    'financial|audited accounts': { status: 'Received', note: 'VDR 2.4' },
    'legal|board minutes': { status: 'To confirm' },
  });
  const t = W.auditTrail({
    findings, dismissed: ['c'], confirmed: ['a', 'b', 'c'], reviewer: ' J. Smith ', date: TODAY, requests,
    treatments: new Map([['a', { key: 'escrow' }], ['b', { key: 'other', text: '' }], ['c', { key: 'price' }]]),
  });
  assert.deepEqual(t.counts, { confirmed: 2, dismissed: 1, treatments: 1, resolved: 1, restatused: 1 });
  assert.deepEqual(t.lines, [
    'Reviewer: J. Smith', `Date: ${TODAY}`, 'Confirmed 2 flags', 'Dismissed 1 finding',
    'Set the treatment on 1 red flag (replacing the suggestion)',
    'Resolved 1 request (Received or Not applicable, each with a note)',
    'Changed the status of 1 other request',
  ]);
});

test('audit trail with no reviewer says so', () => {
  const t = W.auditTrail({ findings: [] });
  assert.equal(t.lines[0], 'Reviewer: not named');
  assert.deepEqual(t.counts, { confirmed: 0, dismissed: 0, treatments: 0, resolved: 0, restatused: 0 });
});
