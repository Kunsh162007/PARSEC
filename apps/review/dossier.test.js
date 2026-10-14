// Run with: node --test apps/review/dossier.test.js
// Inputs are short hand-written strings. These tests check behaviour, not accuracy on real documents.
const test = require('node:test');
const assert = require('node:assert/strict');
const D = require('./dossier.js');
const FP = require('./engine.js');

const contract = {
  name: 'supply-agreement.txt',
  text: 'This Supply Agreement is made between Northwind Traders Ltd and Contoso Holdings Inc. '
    + 'The parties agree that the fee is $2.5 million per year. Termination requires notice. '
    + 'The disclosure is qualified by Schedule 4 and the side letter dated 12 March 2024. '
    + 'Governing law applies. The parties shall indemnify each other.',
};
const accounts = {
  name: 'accounts.txt',
  text: 'Balance sheet of Contoso Holdings Inc. Revenue was $2,500,000 for the year. '
    + 'Net income was lower. Total assets were reported. The cash flow statement follows. Ledger attached.',
};

test('a single document is reported as thin and lists what could not be compared', () => {
  const d = D.build([contract], []);
  assert.equal(d.profile.mode, 'thin');
  assert.ok(d.gaps.some((g) => g.type === 'single-source'));
});

test('references to documents that were not supplied become gaps', () => {
  const d = D.build([contract, accounts], []);
  const labels = d.gaps.filter((g) => g.type === 'referenced-not-supplied').map((g) => g.item.toLowerCase());
  assert.ok(labels.includes('schedule 4'));
  assert.ok(labels.includes('side letter'));
});

test('a referenced schedule that is supplied is not reported as missing', () => {
  const schedule = { name: 'schedule-4.txt', text: 'Schedule 4\nThe fee schedule is set out below.' };
  const d = D.build([contract, schedule], []);
  const ref = d.references.find((r) => r.label.toLowerCase() === 'schedule 4');
  assert.equal(ref.supplied, true);
});

test('the same entity and the same amount are linked across documents', () => {
  const d = D.build([contract, accounts], []);
  const entity = d.entities.find((e) => /contoso/i.test(e.name));
  assert.deepEqual(entity.documents, [0, 1]);
  assert.ok(d.shared_figures.some((f) => f.amount === '$2,500,000'));
});

test('document kind is inferred from content, and unknown when the cues are weak', () => {
  assert.equal(D.classify(contract.text), 'contract');
  assert.equal(D.classify('Lorem ipsum dolor sit amet.'), 'unknown');
});

test('timeline is ordered and understands more than one date style', () => {
  const d = D.build([{ name: 'a', text: 'Signed 12 March 2024. Renewed on 2025-01-05. Earlier: June 3, 2023.' }], []);
  assert.deepEqual(d.timeline.map((e) => e.date), ['2023-06-03', '2024-03-12', '2025-01-05']);
});

test('findings that touch the same entity are threaded together', () => {
  const text = 'Contoso Holdings Inc. may terminate on change of control. Contoso Holdings Inc. accepts unlimited liability.';
  const docs = [{ name: 'c.txt', text }];
  const r = FP.analyze(docs, { lenses: ['legal'], today: '2026-10-10' });
  const d = D.build(docs, r.findings);
  assert.ok(r.findings.length >= 2);
  assert.ok(d.threads.some((t) => /contoso/i.test(t.entity) && t.findings.length >= 2));
});

test('many documents switch on themes, reading order and duplicate detection', () => {
  const docs = [];
  for (let i = 0; i < 9; i += 1) {
    docs.push({ name: `lease-${i}.txt`, text: `Lease ${i}. The tenant has unlimited liability. This lease runs for ten years from the start date shown on the cover page.` });
  }
  docs.push({ name: 'lease-copy.txt', text: docs[0].text });
  const r = FP.analyze(docs, { lenses: ['legal'], today: '2026-10-10' });
  const d = D.build(docs, r.findings);
  assert.equal(d.profile.mode, 'many');
  assert.ok(d.themes.length > 0);
  assert.ok(d.themes.some((t) => t.systemic));
  assert.equal(d.read_first.length, 10);
  assert.ok(d.duplicates.some((p) => p.a === 0 && p.b === 9));
});

test('normal-sized sets do not produce themes or a reading order', () => {
  const d = D.build([contract, accounts], []);
  assert.deepEqual(d.themes, []);
  assert.deepEqual(d.read_first, []);
});

test('role placeholders such as "Public Company" are not treated as parties', () => {
  const d = D.build([{ name: 'a', text: 'Public Company overstated income. Acme Widgets Inc. agreed.' }], []);
  assert.ok(!d.entities.some((e) => /public company/i.test(e.name)));
  assert.ok(d.entities.some((e) => /acme widgets/i.test(e.name)));
});

test('a figure conflict between documents that share no party is marked as such', () => {
  const docs = [
    { name: 'a.txt', text: 'Acme Widgets Inc. reported revenue of $10 million for 2021.' },
    { name: 'b.txt', text: 'Zenith Foods Inc. reported revenue of $4 million for 2021.' },
  ];
  const r = FP.analyze(docs, { lenses: ['financial'], today: '2026-10-10' });
  const d = D.build(docs, r.findings);
  const scope = d.conflict_scope.find((c) => /revenue/i.test(c.title));
  assert.ok(scope, 'expected the engine to raise a revenue conflict');
  assert.equal(scope.sharedParty, false);
});

test('a figure conflict between documents about the same party is marked as shared', () => {
  const docs = [
    { name: 'a.txt', text: 'Acme Widgets Inc. reported revenue of $10 million for 2021.' },
    { name: 'b.txt', text: 'Acme Widgets Inc. reported revenue of $4 million for 2021.' },
  ];
  const r = FP.analyze(docs, { lenses: ['financial'], today: '2026-10-10' });
  const d = D.build(docs, r.findings);
  assert.equal(d.conflict_scope.find((c) => /revenue/i.test(c.title)).sharedParty, true);
});

const FILLER = 'The business sells software subscriptions to mid-sized customers and reports results quarterly. '.repeat(40);
const completeAccounts = {
  name: 'full-accounts.txt',
  text: 'Report of independent registered public accounting firm. Balance sheet. Statement of cash flows. '
    + 'Notes to the consolidated financial statements. Revenue was $5 million in the prior year and $6 million this year. '
    + `Net income was lower. Total assets were reported. Ledger attached. ${FILLER}`,
};

const twoComplete = [completeAccounts, { name: 'second.txt', text: completeAccounts.text }];
const reasoned = (items) => ({ available: true, reason: '', engagement: 'Acquisition of a software company', items });
const item = (name, status, extra) => Object.assign({ name, status, priority: 'high', name_known: true, what_it_is: '', why_needed: 'x' }, extra);

test('without a reasoning result the file can never be cleared', () => {
  const d = D.build(twoComplete, []);
  assert.equal(d.clearance.state, 'incomplete');
  assert.match(d.clearance.reasons.join(' '), /could not be worked out/i);
});

test('an unavailable reasoning model is reported with its reason, not replaced by a default list', () => {
  const d = D.build(twoComplete, [], { requirements: { available: false, reason: 'No reasoning model answered', items: [] } });
  assert.equal(d.clearance.state, 'incomplete');
  assert.match(d.clearance.reasons.join(' '), /No reasoning model answered/);
  assert.equal(d.gaps.filter((g) => g.type === 'required-by-reasoning').length, 0);
});

test('items the model says are missing block clearance and appear as gaps with their explanation', () => {
  const req = reasoned([
    item('Quality of earnings report', 'missing'),
    item('Customer concentration schedule', 'missing', { name_known: false, what_it_is: 'A table of revenue by customer for the last three years.' }),
  ]);
  const d = D.build(twoComplete, [], { requirements: req });
  assert.equal(d.clearance.state, 'incomplete');
  const gap = d.gaps.find((g) => g.item === 'Customer concentration schedule');
  assert.equal(gap.type, 'required-by-reasoning');
  assert.match(gap.whatItIs, /revenue by customer/);
});

test('low-priority missing items are listed but do not block clearance', () => {
  const req = reasoned([item('Organisation chart', 'missing', { priority: 'low' })]);
  const d = D.build(twoComplete, [], { requirements: req });
  assert.equal(d.clearance.state, 'cleared-for-checks');
  assert.ok(d.gaps.some((g) => g.item === 'Organisation chart'));
});

test('clearance is only given when the model found nothing required missing, and it says it is not proof of soundness', () => {
  const d = D.build(twoComplete, [], { requirements: reasoned([item('Audited accounts', 'supplied')]) });
  assert.equal(d.clearance.state, 'cleared-for-checks');
  assert.match(d.clearance.reasons.join(' '), /not evidence/i);
});

test('a reference to a document that was not supplied still blocks clearance', () => {
  const d = D.build([contract, accounts], [], { requirements: reasoned([]) });
  assert.equal(d.clearance.state, 'incomplete');
  assert.ok(d.clearance.unsupplied.length > 0);
});

test('serious findings are reported as issues even when coverage is complete', () => {
  const findings = [{ id: 'x', title: 't', severity: 'high', spans: [] }];
  const d = D.build(twoComplete, findings, { requirements: reasoned([]) });
  assert.equal(d.clearance.state, 'issues');
});

test('many unsupplied exhibits collapse into one line that still blocks clearance', () => {
  const refs = Array.from({ length: 12 }, (_, i) => `Exhibit ${i + 1}`).join('. See ');
  const d = D.build([{ name: '10-K', text: `See ${refs}. ${FILLER}` }, { name: 'b', text: FILLER }], [], { requirements: reasoned([]) });
  const lines = d.gaps.filter((g) => g.type === 'referenced-not-supplied');
  assert.equal(lines.length, 1);
  assert.match(lines[0].item, /^12 exhibits: Exhibit 1, Exhibit 2, Exhibit 3 and 9 more/);
  assert.equal(lines[0].labels.length, 12);
  assert.equal(d.clearance.state, 'incomplete');
});

test('required items carry their workstream into the gap, defaulting to other', () => {
  const req = reasoned([
    item('Quality of earnings report', 'missing', { workstream: 'financial' }),
    item('Organisation chart', 'unclear'),
  ]);
  const d = D.build(twoComplete, [], { requirements: req });
  const ws = Object.fromEntries(d.gaps.filter((g) => g.type === 'required-by-reasoning').map((g) => [g.item, g.workstream]));
  assert.deepEqual(ws, { 'Quality of earnings report': 'financial', 'Organisation chart': 'other' });
});

test('referenced gaps record which documents referred to them', () => {
  const d = D.build([contract, accounts], []);
  const gap = d.gaps.find((g) => g.item.toLowerCase() === 'schedule 4');
  assert.deepEqual(gap.documents, [0]);
});

test('a few unsupplied references are still listed one by one', () => {
  const d = D.build([{ name: 'c', text: `See Schedule 1 and Schedule 2. ${FILLER}` }, { name: 'b', text: FILLER }], [], { requirements: reasoned([]) });
  const items = d.gaps.filter((g) => g.type === 'referenced-not-supplied').map((g) => g.item);
  assert.deepEqual(items.sort(), ['Schedule 1', 'Schedule 2']);
});
