/*
 * Fine Print assistant: turns what a person says or types into one command.
 * "Open Acme", "what's wrong with the customer contract", "next problem", "that's not a problem", "export".
 * Pure text in, command out, so it is instant, works offline and is tested in Node. It never guesses: anything it
 * does not recognise comes back as { action: 'unknown' } and the page says so.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.FinePrintAssistant = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // Words that say how something is asked, not which folder or document it is about.
  const FILLER = new Set(('a an the my our me i you we it its this that these those please can could would will '
    + 'tell show give open check analyse analyze review load start go to of in on at with about for from and or '
    + 'what whats which is are was there here any some all wrong problem problems issue issues risk risks '
    + 'concern concerns flag flags bad file files document documents doc docs folder folders deal deals data room '
    + 'do does have has be see look find me us up now then just').split(' '));

  const AREA_WORDS = {
    financial: ['financial', 'finance', 'money', 'ebitda', 'earnings'],
    legal: ['legal', 'contract', 'contracts', 'law', 'clause', 'clauses'],
    compliance: ['compliance', 'regulatory', 'regulator', 'regulation'],
    tech: ['tech', 'technology', 'security', 'cyber', 'it', 'systems'],
    people: ['people', 'hr', 'staff', 'employees', 'employee', 'workforce'],
    property: ['property', 'environment', 'environmental', 'site', 'sites'],
    tax: ['tax', 'taxes'],
    commercial: ['customer', 'customers', 'market', 'commercial', 'revenue', 'sales'],
    supply: ['supplier', 'suppliers', 'supply', 'esg'],
    integrity: ['integrity'],
  };

  const clean = (text) => String(text || '').toLowerCase().replace(/['’]/g, '').replace(/[^a-z0-9]+/g, ' ').trim();

  // The words of a folder or file name: extension and numbering dropped, separators split.
  function nameTokens(name) {
    const base = String(name || '').replace(/\.[a-z0-9]{2,5}$/i, '');
    return clean(base).split(' ').filter((w) => w.length > 1 && !/^\d+$/.test(w) && !FILLER.has(w));
  }

  const meaningful = (text) => clean(text).split(' ').filter((w) => w.length > 1 && !FILLER.has(w));

  // "contract" matches "contracts", "account" matches "accounts".
  const sameWord = (a, b) => a === b || (Math.min(a.length, b.length) >= 4 && (a.startsWith(b) || b.startsWith(a)));

  // The name that shares the most words with what was said, or null when none shares any.
  function bestMatch(said, names) {
    const words = Array.isArray(said) ? said : meaningful(said);
    let best = null;
    let bestScore = 0;
    let bestRatio = 0;
    for (const name of names || []) {
      const tokens = nameTokens(name);
      if (!tokens.length) continue;
      const score = tokens.filter((t) => words.some((w) => sameWord(w, t))).length;
      const ratio = score / tokens.length;
      if (score > bestScore || (score === bestScore && score > 0 && ratio > bestRatio)) {
        best = name;
        bestScore = score;
        bestRatio = ratio;
      }
    }
    return bestScore > 0 ? best : null;
  }

  function areaOf(text) {
    const words = clean(text).split(' ');
    if (words.includes('all') || words.includes('everything')) return 'all';
    return Object.keys(AREA_WORDS).find((id) => AREA_WORDS[id].some((w) => words.includes(w))) || null;
  }

  const has = (text, re) => re.test(text);

  // "Hey Parsec": the wake phrase, with the spellings speech recognition tends to produce for it. Anywhere in what
  // was heard, since a continuous listener may catch a word or two before it. Returns what was said after it.
  const WAKE = /(?:^|\b)(?:(?:hey|hi|ok|okay|hello)[\s,]+)?(?:parsec|parsecs|parsek|parsex|par sec|pass sec|parse sec|par sek)\b[\s,.!:;-]*(.*)$/i;

  function wake(heard) {
    const m = String(heard || '').match(WAKE);
    return m ? { woke: true, rest: m[1].trim() } : { woke: false, rest: '' };
  }

  const VIEWS = { document: 'document', documents: 'document', page: 'document', findings: 'findings', problems: 'findings',
    questions: 'followups', 'follow ups': 'followups', followups: 'followups', links: 'connections',
    connections: 'connections', tracker: 'workflow', 'deal tracker': 'workflow', workflow: 'workflow' };
  const VIEW_RE = new RegExp(`^(show|open|go to|switch to|take me to)( me)?( the)? (${Object.keys(VIEWS).join('|')})( view| tab| list| page)?$`);

  // Commands about the screen rather than the deal.
  function screenCommand(text) {
    if (/\b(exit|leave|close|stop|turn off|minimi[sz]e|end)\b.*\b(full ?screen|focus)\b|\bnormal (view|size|screen)\b/.test(text)) return { action: 'exit-fullscreen' };
    if (/\bfull ?screen\b|\bmaximi[sz]e\b|\bfocus mode\b/.test(text)) return { action: 'fullscreen' };
    if (/\bzoom in\b|\b(bigger|larger)\b|\bincrease\b.*\b(size|text|font)\b/.test(text)) return { action: 'zoom-in' };
    if (/\bzoom out\b|\bsmaller\b|\bdecrease\b.*\b(size|text|font)\b/.test(text)) return { action: 'zoom-out' };
    if (/\b(scroll|page) down\b/.test(text)) return { action: 'scroll-down' };
    if (/\b(scroll|page) up\b/.test(text)) return { action: 'scroll-up' };
    const view = text.match(VIEW_RE);
    if (view) return { action: 'view', view: VIEWS[view[4]] };
    if (/\b(close|hide) (the )?(assistant|chat|panel)\b/.test(text)) return { action: 'close' };
    if (/\bgo home\b|\bhome page\b|\bback to (the |my )?deals\b|\b(go to|open) my deals\b/.test(text)) return { action: 'go-home' };
    if (/^(please )?print\b/.test(text)) return { action: 'print' };
    if (/^(read (it|this|that)( out| again)?|repeat( that| it)?|say (it|that) again)$/.test(text)) return { action: 'read' };
    return null;
  }

  // A real question ("which…", "why…", "should we…") is for the AI to answer; the phrase match is only a fallback.
  const QUESTION = /^(which|why|how (?!bad)|should|would|could|does|do|did|can|who|when|explain|compare|is it|are they)\b/;

  function parseCommand(input, context) {
    const text = clean(input);
    if (!text) return { action: 'unknown' };
    const command = match(text, context || {});
    return QUESTION.test(text) && command.action !== 'help' ? { action: 'unknown', guess: command } : command;
  }

  function match(text, ctx) {

    const screen = screenCommand(text);
    if (screen) return screen;
    if (has(text, /^(stop|cancel|quiet|be quiet|stop talking|shut up)\b/)) return { action: 'stop' };
    if (has(text, /\b(what can you do|help|commands)\b/)) return { action: 'help' };
    // Only as an instruction: "export" heard in the middle of a sentence is not one.
    if (has(text, /^(please )?(export|download)\b|^(please )?save (the )?marked\b/)) return { action: 'export' };
    if (has(text, /\bnot (a |an )?(problem|issue)|false (alarm|positive|flag)|\bremove (this |that |the )?marks?\b|\bdismiss\b|\bignore (this|that|it)\b|\bwrong flag\b/)) {
      return { action: 'dismiss' };
    }
    if (has(text, /\bbring (it|that|this) back\b|\brestore\b|\bundo\b/)) return { action: 'restore' };
    if (has(text, /\bconfirm\b|\breal problem\b/)) return { action: 'confirm' };
    if (has(text, /\b(list|what|which|show)\b.*\b(folders|deals)\b/) && !bestMatch(text, ctx.deals)) {
      return { action: 'list-deals' };
    }
    if (has(text, /\b(summary|summarise|summarize|overview|how bad)\b/)) return { action: 'summary' };
    if (has(text, /^(next|go next)\b|\bnext (one|problem|issue|point)\b/)) return { action: 'next' };
    if (has(text, /\b(previous|go back|last one)\b|^back\b/)) return { action: 'previous' };

    const area = areaOf(text);
    // Only an instruction filters by area ("show tax problems"); "the customer list" inside a question does not.
    if (area && has(text, /^(please )?(show|only|filter|list|just)\b/)) return { action: 'area', category: area };

    const deal = bestMatch(text, ctx.deals);
    const dealWords = deal ? nameTokens(deal) : [];
    const rest = meaningful(text).filter((w) => !dealWords.some((t) => sameWord(w, t)));
    if (has(text, /\b(wrong|problems?|issues?|risks?|concerns?|red flags?|bad)\b/)) {
      const document = bestMatch(rest, ctx.documents);
      const out = { action: 'problems' };
      if (deal) out.deal = deal;
      if (document) out.document = document;
      return out;
    }
    if (deal && has(text, /\b(open|check|analyse|analyze|review|load|go to|start|look at)\b/)) {
      return { action: 'open-deal', deal };
    }
    const document = bestMatch(rest, ctx.documents);
    if (document && has(text, /\b(open|show|read|go to|look at)\b/)) return { action: 'problems', document };
    return { action: 'unknown' };
  }

  return { parseCommand, bestMatch, areaOf, wake };
});
