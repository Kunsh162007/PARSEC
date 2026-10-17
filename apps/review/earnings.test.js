// Run with: node --test apps/review/earnings.test.js
const test = require('node:test');
const assert = require('node:assert/strict');
const EQ = require('./earnings.js');

const DOCS = [
  { name: '1_cim_acme.txt', text: 'Bridge\n3. Litigation Legal Fees: +$1,200,000 (Non-recurring)\nAdjusted EBITDA: $9,700,000' },
  { name: '2_management_accounts.txt', text: 'Table 1\nFY22 Legal Fees: $1,150,000\nFY23 Legal Fees: $1,180,000' },
  { name: '3_customer_arr.txt', text: 'Northwind Logistics: $4,200,000\nOthers: $5,800,000' },
];
const DEAL = {
  hero_metric: { claimed_ebitda: 9700000, parsec_ebitda: 7600000, delta_ebitda: 2100000, multiple: 10, potential_overpayment_avoided: 21000000 },
  findings: [
    { id: 'FINDING-QOE-AB-2', title: 'Disallowed add-back: Litigation Legal Fees', severity: 'price_adjuster', category: 'QoE',
      description: 'Seller claimed $1,200,000.00 as non-recurring, but the ledger shows the same cost in more than one year.',
      price_impact_usd: 12000000,
      evidence: [
        { document_id: '1_cim_acme.txt', exact_text_quote: '3. Litigation Legal Fees: +$1,200,000 (Non-recurring)' },
        { document_id: '2_management_accounts.txt', exact_text_quote: 'FY22 Legal Fees: $1,150,000' },
      ] },
    { id: 'FINDING-CONC-1', title: 'Customer concentration: Northwind Logistics is 42% of revenue', severity: 'deal_breaker',
      category: 'Concentration', description: 'Northwind is 42% of revenue.', price_impact_usd: 0,
      evidence: [{ document_id: '3_customer_arr.txt', exact_text_quote: 'Northwind Logistics: $4,200,000' }] },
    { id: 'FINDING-X', title: 'Quote not in any document', severity: 'minor', category: 'QoE', description: 'x',
      evidence: [{ document_id: 'missing.txt', exact_text_quote: 'nowhere' }] },
  ],
};

test('documents that talk about EBITDA trigger the earnings check; others do not', () => {
  assert.equal(EQ.looksLikeEarnings(DOCS), true);
  assert.equal(EQ.looksLikeEarnings([{ name: 'nda.txt', text: 'Confidentiality obligations.' }]), false);
});

test('earnings findings point at the exact words, in every document they cite', () => {
  const out = EQ.toFindings(DEAL, DOCS);
  const addback = out.find((f) => f.id === 'earn-FINDING-QOE-AB-2');
  assert.equal(addback.severity, 'high');
  assert.equal(addback.lens, 'financial');
  assert.deepEqual(addback.spans.map((s) => s.doc), [0, 1]);
  const s = addback.spans[1];
  assert.equal(DOCS[1].text.slice(s.start, s.end), 'FY22 Legal Fees: $1,150,000');
  assert.match(addback.why, /\$12\.0m/);
  const conc = out.find((f) => f.id === 'earn-FINDING-CONC-1');
  assert.equal(conc.severity, 'critical');
  assert.equal(conc.lens, 'commercial');
});

test('a finding whose quote cannot be found in the documents is dropped, not shown unanchored', () => {
  assert.equal(EQ.toFindings(DEAL, DOCS).some((f) => f.id === 'earn-FINDING-X'), false);
});

test('the summary line says what the seller claims, what is supported and what it is worth', () => {
  assert.equal(EQ.summaryLine(DEAL),
    'The seller claims adjusted EBITDA of $9.7m; the ledger supports $7.6m. At 10x that is $21.0m of price.');
  assert.equal(EQ.summaryLine({ findings: [] }), '');
});

test('the documents sent are the earnings ones first, within the server limits', () => {
  const many = Array.from({ length: 14 }, (_, i) => ({ name: `d${i}.txt`, text: i === 9 ? 'Adjusted EBITDA 5' : 'x'.repeat(300000) }));
  const sent = EQ.pickDocuments(many);
  assert.equal(Object.keys(sent).length, 10);
  assert.equal(Object.keys(sent)[0], 'd9.txt');
  assert.ok(Object.values(sent).every((t) => t.length <= 200000));
});
