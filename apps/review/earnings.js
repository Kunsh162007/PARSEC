/*
 * Earnings quality inside a document check. When the documents talk about EBITDA, the page sends them to the
 * earnings check (POST /api/v1/data-room/run) and turns what comes back into ordinary findings: each one anchored
 * to the exact words it quotes, in every document it cites. A finding whose quote cannot be found is dropped.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.FinePrintEarnings = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const MAX_DOCS = 10;          // the server's limits for one run
  const MAX_CHARS = 200000;
  const EARNINGS = /\b(?:adjusted\s+ebitda|ebitda|add-?backs?)\b/i;
  const LEDGER = /\b(?:ledger|management accounts|gl account|fy\d{2})\b/i;
  const CUSTOMERS = /\b(?:customers?|clients?|arr)\b/i;

  const SEVERITY = { deal_breaker: 'critical', price_adjuster: 'high', manageable: 'medium', minor: 'low' };
  const ASK = {
    QoE: 'Ask the seller why this cost will not recur, with the invoices for every year it appears in the ledger.',
    Concentration: "Ask for this customer's contract: term, renewal date, termination rights and any change-of-control clause.",
    Security: 'Ask who added this text and for a clean copy of the document.',
  };

  const looksLikeEarnings = (docs) => (docs || []).some((d) => EARNINGS.test(d.text || ''));

  // The documents to send, earnings first, then ledgers, then customer tables, within the server limits.
  function pickDocuments(docs) {
    const rank = (d) => (EARNINGS.test(d.text) ? 0 : LEDGER.test(d.text) ? 1 : CUSTOMERS.test(d.text) ? 2 : 3);
    const ordered = (docs || []).map((d, i) => ({ d, i })).sort((a, b) => rank(a.d) - rank(b.d) || a.i - b.i);
    const out = {};
    for (const { d } of ordered.slice(0, MAX_DOCS)) out[d.name] = String(d.text || '').slice(0, MAX_CHARS);
    return out;
  }

  const millions = (n) => `$${(Number(n) / 1e6).toFixed(1)}m`;

  function spanFor(evidence, docs) {
    const quote = String(evidence.exact_text_quote || '');
    if (!quote) return null;
    const doc = docs.findIndex((d) => d.name === evidence.document_id || d.name === evidence.document_name);
    if (doc < 0) return null;
    const start = docs[doc].text.indexOf(quote);
    return start < 0 ? null : { doc, start, end: start + quote.length };
  }

  // Raw findings in the page's shape (the page adds names and quotes); unanchored ones are dropped.
  function toFindings(deal, docs) {
    const out = [];
    for (const f of (deal && deal.findings) || []) {
      const spans = (f.evidence || []).map((e) => spanFor(e, docs)).filter(Boolean);
      if (!spans.length) continue;
      const impact = Number(f.price_impact_usd) > 0 ? ` At the deal multiple this is about ${millions(f.price_impact_usd)} of price.` : '';
      out.push({
        id: `earn-${f.id}`,
        source: 'earnings',
        lens: /concentration/i.test(f.category) ? 'commercial' : /security/i.test(f.category) ? 'integrity' : 'financial',
        severity: SEVERITY[f.severity] || 'medium',
        title: f.title,
        why: `${f.description}${impact}`,
        ask: ASK[f.category] || 'Ask the seller to explain and evidence this point.',
        spans,
      });
    }
    return out;
  }

  function summaryLine(deal) {
    const h = deal && deal.hero_metric;
    if (!h || !h.claimed_ebitda) return '';
    const impact = h.potential_overpayment_avoided > 0
      ? ` At ${Number(h.multiple)}x that is ${millions(h.potential_overpayment_avoided)} of price.` : '';
    return `The seller claims adjusted EBITDA of ${millions(h.claimed_ebitda)}; the ledger supports ${millions(h.parsec_ebitda)}.${impact}`;
  }

  return { looksLikeEarnings, pickDocuments, toFindings, summaryLine };
});
