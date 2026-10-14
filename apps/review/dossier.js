/*
 * Fine Print dossier layer.
 * Takes the documents and the findings from engine.js and connects them: who and what appears across documents,
 * which figures recur, what is referred to but not supplied, what could not be tested, and, when there are many
 * documents, which ones to read first and which problems repeat. Domain-neutral: nothing here assumes finance.
 * Pure text in, plain objects out. Works in the browser (window.FinePrintDossier) and in Node.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.FinePrintDossier = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const MANY_DOCS = 8;
  const MANY_FINDINGS = 40;
  const SHORT_WORDS = 500;
  const MAX_ITEMS = 25;
  const COLLAPSE_REFERENCES = 5;
  const DUPLICATE_SIMILARITY = 0.9;
  const SEVERITY_WEIGHT = { critical: 8, high: 4, medium: 2, low: 1 };
  const RANK = { critical: 0, high: 1, medium: 2, low: 3 };

  const KIND_CUES = [
    ['contract', /\b(?:agreement|hereinafter|whereas|governing law|the parties|party|termination|indemnif\w+)\b/gi],
    ['financial', /\b(?:balance sheet|income statement|cash flow|revenue|ebitda|total assets|net income|ledger|invoice)\b/gi],
    ['policy', /\b(?:policy|procedure|shall comply|responsibilit\w+|code of conduct|standard operating)\b/gi],
    ['legal-order', /\b(?:respondent|complaint|plaintiff|defendant|commission|proceeding|court|order)\b/gi],
    ['technical', /\b(?:vulnerabilit\w+|penetration|scan|architecture|patch|encryption|uptime|incident)\b/gi],
    ['property', /\b(?:premises|lease|tenant|landlord|title|survey|zoning|environmental)\b/gi],
    ['people', /\b(?:employee|salary|headcount|employment|payroll|benefits|redundan\w+)\b/gi],
    ['correspondence', /^(?:from|to|subject|sent|cc):/gim],
  ];

  const ENTITY_RE = /\b((?:[A-Z][\w&'’.-]*)(?:\s+[A-Z][\w&'’.-]*){0,4})\s+(Inc\.?|Corp\.?|Corporation|LLC|L\.L\.C\.|Ltd\.?|Limited|LLP|plc|GmbH|S\.A\.|N\.V\.|AG|Co\.|Company|Holdings|Group|Bank|Partners|Trust)(?=[\s,.;:)]|$)/g;
  const ENTITY_STOP = new Set(['the', 'this', 'that', 'such', 'each', 'any', 'and', 'or', 'of', 'by', 'between', 'with', 'for', 'to', 'in', 'on', 'a', 'an']);
  // Placeholders that name a role, not a party (regulators anonymise parties this way).
  const GENERIC_ENTITIES = new Set(['public company', 'the company', 'company', 'issuer company', 'parent company', 'holding company', 'target company', 'a company', 'our company']);
  const REFERENCE_RE =/\b(Schedule|Exhibit|Annex(?:ure)?|Appendix|Attachment|Addendum|Amendment No\.?)\s+([A-Z]?\d+(?:\.\d+)?[A-Za-z]?|[A-Z])\b/g;
  const NAMED_DOC_RE = /\b(side letter|disclosure schedules?|management letter|board minutes|data room index|cap table|master services agreement|audit(?:ed)? (?:report|accounts)|compliance certificate|bank confirmation|term sheet)\b/gi;
  const MONEY_RE = /([$€£])\s?(\d[\d,]*(?:\.\d+)?)\s?(million|billion|thousand|bn|mm|[mMbBkK])?\b/g;
  const DATE_RE = /\b(?:(\d{4})-(\d{2})-(\d{2})|(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})|(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4}))\b/g;
  const MONTHS = ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october', 'november', 'december'];
  const SCALE = { million: 1e6, mm: 1e6, m: 1e6, billion: 1e9, bn: 1e9, b: 1e9, thousand: 1e3, k: 1e3 };

  const words = (text) => (text.match(/\S+/g) || []).length;
  const snippet = (text, start, end, pad = 70) =>
    text.slice(Math.max(0, start - pad), Math.min(text.length, end + pad)).replace(/\s+/g, ' ').trim();
  const entityKey = (name) => name.toLowerCase().replace(/[^a-z0-9 ]/g, '').replace(/\s+/g, ' ').trim();

  function classify(text) {
    let best = ['unknown', 0];
    for (const [kind, re] of KIND_CUES) {
      const hits = (text.match(re) || []).length;
      if (hits > best[1]) best = [kind, hits];
    }
    return best[1] >= 3 ? best[0] : 'unknown';
  }

  function extractEntities(docs) {
    const map = new Map();
    for (const doc of docs) {
      ENTITY_RE.lastIndex = 0;
      let m;
      while ((m = ENTITY_RE.exec(doc.text))) {
        const lead = m[1].split(/\s+/);
        while (lead.length && ENTITY_STOP.has(lead[0].toLowerCase())) lead.shift();
        if (!lead.length) continue;
        const name = `${lead.join(' ')} ${m[2]}`;
        const key = entityKey(name);
        if (GENERIC_ENTITIES.has(key)) continue;
        if (!map.has(key)) map.set(key, { name, docs: new Set(), mentions: 0, firstSnippet: snippet(doc.text, m.index, m.index + m[0].length) });
        const e = map.get(key);
        e.docs.add(doc.index);
        e.mentions += 1;
      }
    }
    return [...map.values()]
      .map((e) => ({ name: e.name, documents: [...e.docs].sort((a, b) => a - b), mentions: e.mentions, context: e.firstSnippet }))
      .sort((a, b) => b.documents.length - a.documents.length || b.mentions - a.mentions);
  }

  function sharedFigures(docs) {
    const byValue = new Map();
    for (const doc of docs) {
      MONEY_RE.lastIndex = 0;
      let m;
      while ((m = MONEY_RE.exec(doc.text))) {
        const value = parseFloat(m[2].replace(/,/g, '')) * (SCALE[(m[3] || '').toLowerCase()] || 1);
        if (!Number.isFinite(value) || value < 1000) continue;
        const key = `${m[1]}${value}`;
        if (!byValue.has(key)) byValue.set(key, { value, currency: m[1], places: [] });
        byValue.get(key).places.push({ doc: doc.index, snippet: snippet(doc.text, m.index, m.index + m[0].length) });
      }
    }
    return [...byValue.values()]
      .filter((f) => new Set(f.places.map((p) => p.doc)).size >= 2)
      .sort((a, b) => b.value - a.value)
      .slice(0, MAX_ITEMS)
      .map((f) => ({ amount: `${f.currency}${f.value.toLocaleString('en-US')}`, documents: [...new Set(f.places.map((p) => p.doc))], places: f.places.slice(0, 4) }));
  }

  function timeline(docs) {
    const events = [];
    for (const doc of docs) {
      DATE_RE.lastIndex = 0;
      let m;
      while ((m = DATE_RE.exec(doc.text))) {
        let y; let mo; let d;
        if (m[1]) { y = +m[1]; mo = +m[2]; d = +m[3]; }
        else if (m[4]) { y = +m[6]; mo = MONTHS.indexOf(m[5].toLowerCase()) + 1; d = +m[4]; }
        else { y = +m[9]; mo = MONTHS.indexOf(m[7].toLowerCase()) + 1; d = +m[8]; }
        if (y < 1950 || y > 2100 || mo < 1 || mo > 12 || d < 1 || d > 31) continue;
        const iso = `${y}-${String(mo).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
        events.push({ date: iso, doc: doc.index, snippet: snippet(doc.text, m.index, m.index + m[0].length, 60) });
      }
    }
    events.sort((a, b) => a.date.localeCompare(b.date));
    return events.slice(0, MAX_ITEMS * 2);
  }

  function provided(label, docs) {
    const needle = label.toLowerCase();
    const lineStart = new RegExp(`^\\s*${label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`, 'im');
    return docs.some((d) => d.name.toLowerCase().includes(needle) || lineStart.test(d.text));
  }

  function referencedDocuments(docs) {
    const found = new Map();
    const add = (label, doc, index, length) => {
      const key = label.toLowerCase();
      if (!found.has(key)) found.set(key, { label, mentionedIn: new Set(), context: snippet(doc.text, index, index + length) });
      found.get(key).mentionedIn.add(doc.index);
    };
    for (const doc of docs) {
      REFERENCE_RE.lastIndex = 0;
      NAMED_DOC_RE.lastIndex = 0;
      let m;
      while ((m = REFERENCE_RE.exec(doc.text))) add(`${m[1]} ${m[2]}`, doc, m.index, m[0].length);
      while ((m = NAMED_DOC_RE.exec(doc.text))) add(m[1].toLowerCase(), doc, m.index, m[0].length);
    }
    return [...found.values()].map((r) => ({
      label: r.label,
      documents: [...r.mentionedIn],
      context: r.context,
      supplied: provided(r.label, docs),
    }));
  }

  // Gaps come from two places only: references inside the documents themselves, and the model's judgement of what
  // this kind of file needs (supplied by the server). No fixed list lives here.
  function gapList(references, docs, required) {
    const gaps = [];
    // Filings cite dozens of exhibits filed elsewhere. More than a few of one kind become a single line.
    const byKind = new Map();
    for (const r of references.filter((x) => !x.supplied)) {
      const kind = r.label.split(' ')[0].toLowerCase();
      if (!byKind.has(kind)) byKind.set(kind, []);
      byKind.get(kind).push(r);
    }
    for (const [kind, refs] of byKind) {
      const where = [...new Set(refs.flatMap((r) => r.documents))].map((i) => docs[i].name).join(', ');
      if (refs.length > COLLAPSE_REFERENCES) {
        const shown = refs.slice(0, 3).map((r) => r.label).join(', ');
        gaps.push({
          type: 'referenced-not-supplied',
          item: `${refs.length} ${kind === 'exhibit' ? 'exhibits' : `${kind} references`}: ${shown} and ${refs.length - 3} more`,
          labels: refs.map((r) => r.label),
          documents: [...new Set(refs.flatMap((r) => r.documents))],
          why: `Referred to in ${where} but not supplied. Filings often attach these separately; ask for the ones that matter.`,
          blocks: 'Anything these documents say or qualify cannot be confirmed from what was supplied.',
        });
        continue;
      }
      for (const r of refs.slice(0, MAX_ITEMS)) {
        gaps.push({
          type: 'referenced-not-supplied',
          item: r.label,
          labels: [r.label],
          documents: r.documents,
          why: `Referred to in ${r.documents.map((i) => docs[i].name).join(', ')} but no document or section with that label was provided.`,
          blocks: 'Anything this document says or qualifies cannot be confirmed from what was supplied.',
        });
      }
    }
    for (const item of (required && required.available ? required.items : []).filter((x) => x.status !== 'supplied')) {
      gaps.push({
        type: 'required-by-reasoning',
        item: item.name,
        whatItIs: item.name_known ? '' : item.what_it_is,
        why: item.why_needed || item.what_it_is,
        priority: item.priority,
        status: item.status,
        workstream: item.workstream || 'other',
        blocks: item.status === 'unclear'
          ? 'It may be present but could not be confirmed from the material shown.'
          : 'Not among the documents supplied.',
      });
    }
    if (docs.length < 2) {
      gaps.push({
        type: 'single-source',
        item: 'A second, independent source',
        why: 'Only one document was supplied, so figures and claims could not be compared against anything else.',
        blocks: 'Cross-document conflicts, shared parties and repeated figures cannot be tested.',
      });
    }
    return gaps;
  }

  function entityContextOf(doc, span, entities) {
    const text = doc.text.slice(Math.max(0, span.start - 250), Math.min(doc.text.length, span.end + 250)).toLowerCase();
    return entities.filter((e) => text.includes(e.name.toLowerCase())).map((e) => entityKey(e.name));
  }

  function threads(docs, findings, entities) {
    const byEntity = new Map();
    for (const f of findings) {
      const keys = new Set();
      for (const s of f.spans || []) {
        if (!docs[s.doc]) continue;
        entityContextOf(docs[s.doc], s, entities).forEach((k) => keys.add(k));
      }
      for (const k of keys) {
        if (!byEntity.has(k)) byEntity.set(k, []);
        byEntity.get(k).push(f);
      }
    }
    const nameOf = new Map(entities.map((e) => [entityKey(e.name), e.name]));
    return [...byEntity.entries()]
      .filter(([, list]) => list.length >= 2)
      .map(([key, list]) => ({
        entity: nameOf.get(key),
        findings: list.map((f) => ({ id: f.id, title: f.title, severity: f.severity })),
        documents: [...new Set(list.flatMap((f) => (f.spans || []).map((s) => s.doc)))],
      }))
      .sort((a, b) => b.findings.length - a.findings.length)
      .slice(0, MAX_ITEMS);
  }

  // A figure conflict only means something when both documents are about the same party. When they share no
  // named party, say so, so the reader does not chase a difference between two unrelated companies.
  function conflictScope(findings, entities) {
    const out = [];
    for (const f of findings) {
      if (!/^int-conflict-/.test(f.id)) continue;
      const docsInvolved = [...new Set((f.spans || []).map((s) => s.doc))];
      if (docsInvolved.length < 2) continue;
      const shared = entities.some((e) => docsInvolved.every((d) => e.documents.includes(d)));
      out.push({ id: f.id, title: f.title, documents: docsInvolved, sharedParty: shared });
    }
    return out;
  }

  const baseId = (id) => String(id).replace(/-\d+$/, '');

  function themes(docs, findings) {
    const map = new Map();
    for (const f of findings) {
      const key = baseId(f.id);
      if (!map.has(key)) map.set(key, { id: key, title: f.title, severity: f.severity, docs: new Set(), count: 0 });
      const t = map.get(key);
      t.count += f.count || 1;
      if (RANK[f.severity] < RANK[t.severity]) t.severity = f.severity;
      (f.spans || []).forEach((s) => t.docs.add(s.doc));
    }
    return [...map.values()]
      .map((t) => ({ id: t.id, title: t.title, severity: t.severity, documents: t.docs.size, occurrences: t.count, systemic: t.docs.size >= 3 }))
      .sort((a, b) => RANK[a.severity] - RANK[b.severity] || b.documents - a.documents || b.occurrences - a.occurrences);
  }

  function readFirst(docs, findings) {
    const score = new Map(docs.map((d) => [d.index, 0]));
    for (const f of findings) {
      for (const doc of new Set((f.spans || []).map((s) => s.doc))) {
        score.set(doc, (score.get(doc) || 0) + (SEVERITY_WEIGHT[f.severity] || 1));
      }
    }
    return docs
      .map((d) => ({ index: d.index, name: d.name, score: score.get(d.index) || 0 }))
      .sort((a, b) => b.score - a.score)
      .slice(0, 10);
  }

  function shingles(text) {
    const toks = text.toLowerCase().match(/[a-z0-9]+/g) || [];
    const set = new Set();
    for (let i = 0; i + 5 <= toks.length; i += 1) set.add(toks.slice(i, i + 5).join(' '));
    return set;
  }

  function duplicates(docs) {
    const sets = docs.map((d) => shingles(d.text));
    const out = [];
    for (let i = 0; i < docs.length; i += 1) {
      for (let j = i + 1; j < docs.length; j += 1) {
        const a = sets[i]; const b = sets[j];
        if (!a.size || !b.size) continue;
        let inter = 0;
        for (const s of a) if (b.has(s)) inter += 1;
        const sim = inter / (a.size + b.size - inter);
        if (sim >= DUPLICATE_SIMILARITY) out.push({ a: i, b: j, similarity: Math.round(sim * 100) / 100 });
      }
    }
    return out;
  }

  // Never says a company or case is sound. It says whether, for the documents supplied, the checks could run and
  // whether what the file needs could be established and was found.
  function clearance(findings, required, gaps, mode) {
    const serious = findings.filter((f) => f.severity === 'critical' || f.severity === 'high').length;
    const blocking = (required && required.available ? required.items : [])
      .filter((x) => x.status !== 'supplied' && x.priority !== 'low')
      .map((x) => x.name);
    const unsupplied = gaps.filter((g) => g.type === 'referenced-not-supplied').map((g) => g.item);
    const reasons = [];
    if (!required || !required.available) {
      reasons.push(`What this file needs could not be worked out${required && required.reason ? ` (${required.reason})` : ' (no reasoning model was asked)'}, so completeness cannot be confirmed.`);
    }
    if (mode === 'thin') reasons.push('Very little material, or a single source, so cross-document checks could not run.');
    if (blocking.length) reasons.push(`Still needed: ${blocking.join('; ')}.`);
    if (unsupplied.length) reasons.push(`Referred to but not supplied: ${unsupplied.join('; ')}.`);
    const incomplete = reasons.length > 0;
    if (serious) {
      return { state: 'issues', headline: incomplete ? 'Issues found. Coverage is also incomplete.' : 'Issues found.', reasons, missing: blocking, unsupplied };
    }
    if (incomplete) {
      return { state: 'incomplete', headline: 'Cannot clear this yet. Documents or sections are missing.', reasons, missing: blocking, unsupplied };
    }
    return {
      state: 'cleared-for-checks',
      headline: 'No serious issues in the documents supplied, for the checks run.',
      reasons: ['This is not evidence that the company or case is sound. It covers only what was supplied and only these checks.'],
      missing: blocking, unsupplied,
    };
  }

  function build(documents, findings, options) {
    const required = (options && options.requirements) || null;
    const docs = (documents || []).map((d, i) => ({
      index: i,
      name: d.name || `Document ${i + 1}`,
      text: String(d.text || '').replace(/\r\n?/g, '\n'),
    }));
    const list = findings || [];
    const profileDocs = docs.map((d) => ({ index: d.index, name: d.name, kind: classify(d.text), words: words(d.text) }));
    const totalWords = profileDocs.reduce((a, d) => a + d.words, 0);
    const entities = extractEntities(docs);
    const references = referencedDocuments(docs);
    const mode = docs.length >= MANY_DOCS || list.length >= MANY_FINDINGS ? 'many'
      : docs.length < 2 || totalWords < SHORT_WORDS ? 'thin' : 'normal';

    const gaps = gapList(references, docs, required);

    return {
      clearance: clearance(list, required, gaps, mode),
      requirements: required && required.available ? { engagement: required.engagement, items: required.items } : null,
      profile: { documents: docs.length, words: totalWords, mode },
      documents: profileDocs,
      entities: entities.slice(0, MAX_ITEMS),
      shared_figures: sharedFigures(docs),
      timeline: timeline(docs),
      references,
      gaps,
      threads: threads(docs, list, entities),
      conflict_scope: conflictScope(list, entities),
      themes: mode === 'many' ? themes(docs, list) : [],
      read_first: mode === 'many' ? readFirst(docs, list) : [],
      duplicates: docs.length >= 2 && docs.length <= 60 ? duplicates(docs) : [],
    };
  }

  return { build, classify, extractEntities, MANY_DOCS, MANY_FINDINGS };
});
