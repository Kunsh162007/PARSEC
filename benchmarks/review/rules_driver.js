// Runs the real Fine Print rules engine over benchmark documents.
// stdin:  JSON [{ "id": "...", "text": "..." }, ...]
// stdout: JSON { "<id>": [ { id, severity, title, lens, spans: [{doc, start, end}] }, ... ], ... }
const path = require('path');
const FP = require(path.join(__dirname, '..', '..', 'apps', 'review', 'engine.js'));

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => { input += chunk; });
process.stdin.on('end', () => {
  const docs = JSON.parse(input);
  const out = {};
  for (const doc of docs) {
    const result = FP.analyze([{ name: doc.id, text: doc.text }], { allSpans: true });
    out[doc.id] = result.findings.map((f) => ({
      id: f.id, severity: f.severity, title: f.title, lens: f.lens, spans: f.spans,
    }));
  }
  process.stdout.write(JSON.stringify(out));
});
