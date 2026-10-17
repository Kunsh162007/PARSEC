/*
 * Runs the Fine Print rules engine (apps/review/engine.js) for the API, so the server and the browser can never
 * disagree. Reads one JSON request on stdin, writes one JSON reply on stdout:
 *   {"op": "categories"}                                   -> {"categories": [...]}
 *   {"op": "analyze", "documents": [...], "lenses": [...], "today": "YYYY-MM-DD"} -> engine result
 * Offsets in the reply are Unicode code points, as Python counts them, not JavaScript's UTF-16 units.
 */
'use strict';

const path = require('path');
const FP = require(path.join(__dirname, '..', '..', 'apps', 'review', 'engine.js'));

const codePoints = (text, units) => Array.from(String(text).slice(0, units)).length;

function analyze(req) {
  const docs = (req.documents || []).map((d) => ({ name: String(d.name || ''), text: String(d.text || '') }));
  const result = FP.analyze(docs, { lenses: req.lenses || [], today: req.today || undefined });
  const findings = result.findings.map((f) => {
    const spans = f.spans.map((s) => {
      const text = docs[s.doc].text;
      return { doc: s.doc, start: codePoints(text, s.start), end: codePoints(text, s.end) };
    });
    return Object.assign({}, f, { spans, context: f.quote ? f.quote.text : '' });
  });
  return { findings, stats: result.stats };
}

function categories() {
  return { categories: [FP.INTEGRITY, ...FP.LENSES].map((l) => ({ id: l.id, name: l.name, description: l.blurb })) };
}

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => { input += chunk; });
process.stdin.on('end', () => {
  try {
    const req = JSON.parse(input || '{}');
    const out = req.op === 'categories' ? categories() : analyze(req);
    process.stdout.write(JSON.stringify(out));
  } catch (err) {
    process.stderr.write(String(err && err.message ? err.message : err));
    process.exit(1);
  }
});
