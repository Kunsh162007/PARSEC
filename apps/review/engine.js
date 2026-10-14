/*
 * Fine Print review engine.
 * Pure text in, findings out. Works in the browser (window.FinePrint) and in Node.
 * Every finding points at exact character spans so the UI can highlight them.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.FinePrint = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const SEVERITIES = ['critical', 'high', 'medium', 'low'];
  const RANK = { critical: 0, high: 1, medium: 2, low: 3 };
  const MAX_SPANS = 6;
  const MAX_HITS_PER_DOC = 25;
  const DAY = 86400000;

  const LENSES = [
    { id: 'financial', name: 'Financial', blurb: "Figures that don't add up, adjusted-profit claims, audit qualifications, covenants and cash stress." },
    { id: 'legal', name: 'Legal and contracts', blurb: 'Change-of-control triggers, uncapped liability, exclusivity, missing clauses and open disputes.' },
    { id: 'compliance', name: 'Compliance and regulatory', blurb: 'Regulator actions, lapsed licences, privacy breaches, sanctions and bribery exposure.' },
    { id: 'tech', name: 'Technology and security', blurb: 'Unsupported systems, missing controls, past incidents, exposed secrets and open-source licence risk.' },
    { id: 'people', name: 'People and HR', blurb: 'Attrition, key-person exits, change-of-control payments, labour claims and worker classification.' },
    { id: 'property', name: 'Property and environment', blurb: 'Title defects, contamination, zoning and permit gaps, lease terms and physical condition.' },
    { id: 'tax', name: 'Tax', blurb: 'Open audits, transfer pricing, indirect-tax gaps and positions a tax authority could challenge.' },
    { id: 'commercial', name: 'Customers and market', blurb: 'Customer concentration, churn, pilots counted as revenue, lost accounts and stretched forecasts.' },
    { id: 'supply', name: 'Suppliers and ESG', blurb: 'Single-source dependency, labour and environmental breaches, unverified green claims.' },
  ];
  const INTEGRITY = {
    id: 'integrity',
    name: 'Document integrity',
    blurb: 'Always on: placeholders, blank signatures, expired dates, conflicting figures, withheld pages and hidden instructions.',
  };
  const LENS_NAME = Object.fromEntries([...LENSES, INTEGRITY].map((l) => [l.id, l.name]));
  const LENS_ORDER = Object.fromEntries([INTEGRITY, ...LENSES].map((l, i) => [l.id, i]));

  /* ------------------------------------------------------------------ */
  /* Keyword rules                                                       */
  /* ------------------------------------------------------------------ */

  // neg:false means the pattern already expresses the problem, so "no" in front is part of it.
  const R = (id, lens, severity, title, re, why, ask, neg) => ({ id, lens, severity, title, re, why, ask, neg: neg !== false });

  const RULES = [
    /* ---- document integrity ---- */
    R('int-placeholder', 'integrity', 'medium', 'Unfinished text left in the document',
      /\[(?:●|\*|insert[^\]]{0,40}|tbd|tbc|name|date|amount|company|xx+)\]|\bTBD\b|\bTBC\b|\bXX{2,}\b|lorem ipsum|\bto be (?:agreed|confirmed|determined|completed)\b|\{\{[^}]+\}\}/gi,
      'Placeholders mean the document was never finished or reviewed, so terms or numbers may still change.',
      'Ask for the final, completed version and what each blank was meant to say.', false),
    R('int-blank-sign', 'integrity', 'high', 'Blank signature or date line',
      /\b(?:signature|signed|sign(?:ed)? by|date|name|title|witness)\s*[:.]?\s*_{3,}/gi,
      'An unsigned or undated document may not be binding, or may not be the version that was actually agreed.',
      'Ask for the fully executed copy with every signature and date filled in.', false),
    R('int-draft', 'integrity', 'medium', 'Marked as draft, preliminary or subject to change',
      /\bDRAFT\b|for discussion (?:purposes )?only|subject to (?:contract|board approval|final (?:review|approval)|verification|change)|not (?:yet )?(?:final|approved|signed|executed|reviewed)|work in progress|\bWIP\b|preliminary (?:figures|numbers|results|draft|version)|unreviewed/g,
      'A draft can change before it is final, and numbers in it may not have been checked by anyone.',
      'Confirm whether a final version exists and what changed since this one.'),
    R('int-withheld', 'integrity', 'high', 'Information redacted, missing or withheld',
      /\[?redacted\]?|█{2,}|\[withheld\]|(?:information|content|pages?|sections?|schedules?|annex(?:es|ures?)?|exhibits?) (?:has |have |was |were )?(?:been )?(?:removed|withheld|omitted|redacted|missing)|refused to (?:provide|disclose|share)|declined to (?:provide|disclose|share)|(?:intentionally )?left blank|not (?:provided|disclosed|made available)/gi,
      'Missing material is where problems tend to sit. A gap is a finding in its own right.',
      'Ask for the unredacted version, or a written reason it cannot be shared.', false),
    R('int-unverified', 'integrity', 'medium', 'Statements nobody has verified',
      /to the best of (?:our|the company'?s?|management'?s?|its|my) knowledge|management (?:believes|estimates)|as far as (?:we|management) (?:are|is) aware|not (?:been )?(?:independently )?verified|subject to (?:verification|confirmation)|unconfirmed|anecdotal|ballpark|rough(?:ly)? (?:estimate|number)/gi,
      "Knowledge qualifiers and unverified claims shift the risk to you: they are only as good as the person's awareness.",
      'Ask what evidence supports each statement and who checked it.', false),
    R('int-absolute', 'integrity', 'low', 'Absolute or too-good-to-be-true claims',
      /\b(?:never|always|guaranteed?|zero) (?:risk|issues?|problems?|defects?|complaints?|incidents?|claims?|disputes?|churn|downtime)\b|100\s?% (?:compliant|compliance|secure|uptime|retention|satisfaction|accurate)|risk[- ]free|fully (?:compliant|secure|insured)|no material (?:adverse|issues?|risks?|liabilit\w+|litigation)/gi,
      'Real businesses rarely have zero of anything. Absolute claims are usually untested.',
      'Ask for the records or tests behind the claim.', false),
    R('int-superlative', 'integrity', 'low', 'Unsupported superlatives',
      /\b(?:market[- ]leading|market leader|best[- ]in[- ]class|world[- ]class|industry[- ]leading|unrivall?ed|first[- ]of[- ]its[- ]kind|number one|leading provider|only (?:company|provider|solution) (?:that|to|which))\b/gi,
      'Superlatives are marketing, not evidence.',
      'Ask for independent data (analyst reports, rankings, customer references) behind each claim.'),

    /* ---- financial ---- */
    R('fin-going-concern', 'financial', 'critical', 'Going-concern doubt',
      /going[- ]concern|substantial doubt|material uncertainty/gi,
      'Auditors or management are saying the business may not survive the next 12 months.',
      'Request the going-concern assessment, the 12-month cash-flow forecast and any lender correspondence.'),
    R('fin-audit-opinion', 'financial', 'critical', 'Qualified or adverse audit opinion',
      /(?<!un)qualified opinion|adverse opinion|disclaimer of opinion|except for the effects/gi,
      'The auditor did not give a clean sign-off on the accounts.',
      "Request the full audit report, management letter and the auditor's reasons.", false),
    R('fin-restatement', 'financial', 'high', 'Restated or corrected figures',
      /restat(?:ed|ement|ing)|prior[- ]period adjustment|correction of (?:an )?error/gi,
      'Earlier numbers were wrong or were changed. That raises the question of what else is.',
      'Get the original and restated figures side by side with the reason for each change.'),
    R('fin-weakness', 'financial', 'high', 'Weaknesses in financial controls',
      /material weakness|significant deficienc(?:y|ies)|control deficienc(?:y|ies)/gi,
      'The systems that produce the numbers are not reliable enough to trust without testing.',
      'Ask for the control deficiency log and the remediation status.'),
    R('fin-unaudited', 'financial', 'medium', 'Figures are unaudited',
      /unaudited|not been audited|management accounts only|draft accounts/gi,
      'No independent party has checked these numbers.',
      'Ask for audited statements, or a recent review or agreed-upon-procedures report.', false),
    R('fin-adjusted', 'financial', 'medium', 'Adjusted, pro-forma or run-rate figures',
      /adjusted (?:EBITDA|earnings|net income|profit|operating income)|pro[- ]forma|run[- ]rate|normali[sz]ed|annuali[sz]ed|add[- ]?backs?/gi,
      'Non-standard measures move with management choices. They need rebuilding from the ledger.',
      'Request a bridge from reported to adjusted figures with support for every adjustment.', false),
    R('fin-oneoff', 'financial', 'high', 'Costs labelled one-off or non-recurring',
      /one[- ]?off|non[- ]?recurring|exceptional (?:item|cost|expense)s?|extraordinary (?:item|cost|expense)s?/gi,
      'Costs called one-off are often repeated year after year, which flatters profit.',
      "Check whether the same item appears in each prior year's accounts."),
    R('fin-related', 'financial', 'high', 'Related-party dealings',
      /related[- ]part(?:y|ies)|affiliate transactions?|arm'?s[- ]length|connected (?:person|compan)\w*/gi,
      'Deals with insiders can be priced to move profit or cash between entities.',
      'Request a list of all related-party transactions with terms and evidence of market pricing.'),
    R('fin-covenant', 'financial', 'high', 'Debt covenant or default issue',
      /covenant[^.\n]{0,80}(?:breach|waiv|default|non[- ]compliance|headroom|reset)|(?:breach|waiv\w+)[^.\n]{0,40}covenant|event of default/gi,
      'A covenant problem can let lenders demand repayment or change terms.',
      'Ask for covenant compliance certificates, waivers and the latest lender correspondence.'),
    R('fin-contingent', 'financial', 'medium', 'Contingent or off-balance-sheet obligations',
      /contingent liabilit(?:y|ies)|off[- ]balance[- ]sheet|undisclosed liabilit(?:y|ies)|letters? of credit|guarantee[sd]? (?:of|for)/gi,
      'Obligations that sit outside the headline numbers can still become real cash costs.',
      'Request a full schedule of guarantees, commitments and contingent items with the exposure for each.'),
    R('fin-revrec', 'financial', 'high', 'Aggressive revenue-recognition signals',
      /bill[- ]and[- ]hold|channel stuffing|side letters?|round[- ]trip|revenue recogni[sz]ed (?:up[- ]?front|in advance|before)|extended payment terms|on consignment/gi,
      'These are classic ways to bring revenue forward or inflate it.',
      'Ask for the revenue-recognition policy and the contracts behind the largest recognised amounts.'),
    R('fin-receivables', 'financial', 'medium', 'Receivables collection problems',
      /receivables?[^.\n]{0,60}(?:overdue|past due|aged|doubtful|write[- ]?offs?|provision)|bad debts?|DSO[^.\n]{0,20}(?:increas|rose|up)/gi,
      'Revenue that is not collected is not cash.',
      'Request an aged receivables listing and subsequent cash receipts testing.'),
    R('fin-liquidity', 'financial', 'high', 'Cash or liquidity pressure',
      /cash (?:runway|burn)|runway of|months? of (?:cash|runway)|overdraft|liquidity (?:shortfall|constraint|pressure)|negative (?:working capital|cash flow)|unable to pay|late payments? to suppliers/gi,
      'A business short of cash makes different decisions: delaying costs, discounting, stretching suppliers.',
      'Request the 13-week cash-flow forecast and a list of overdue supplier balances.'),
    R('fin-capitalised', 'financial', 'medium', 'Costs capitalised instead of expensed',
      /capitali[sz]ed (?:development|software|R&D|costs?|salaries)|capitali[sz]ation of/gi,
      'Capitalising costs lifts profit today and defers the hit.',
      'Ask for the capitalisation policy and a schedule of what was capitalised and why.'),

    /* ---- legal and contracts ---- */
    R('leg-coc', 'legal', 'high', 'Change-of-control or assignment trigger',
      /change[- ]of[- ]control|change in control|assign(?:ment)? (?:of|without|requires?)[^.\n]{0,40}consent|consent[^.\n]{0,40}assign/gi,
      'The deal itself may let the other side terminate, renegotiate or demand consent.',
      'List every contract with a change-of-control or anti-assignment clause and the consent each needs.'),
    R('leg-tfc', 'legal', 'medium', 'Can be ended on notice, with or without a reason',
      /terminat\w*[^.]{0,140}(?:at any time|for any reason|for convenience|without cause|without reason|upon (?:\w+ )?\(?\d+\)?(?:[- ]day)? ?(?:calendar )?(?:days?|months?) ?(?:'s)? ?(?:prior )?(?:written )?notice)|(?:upon|on|with) (?:\w+ )?\(?\d+\)?[- ](?:calendar )?(?:days?|months?)'? (?:prior )?(?:written )?notice[^.]{0,80}terminat/gi,
      'Revenue or supply that either side can end on notice is less secure than it looks.',
      'Ask for notice periods and how often these rights have been used.', false),
    R('leg-assignment', 'legal', 'medium', 'Contract cannot be transferred without consent',
      /(?:may not|shall not|cannot|no party (?:may|shall|will)|neither (?:party|this agreement)[^.]{0,80}(?:may|shall|will))[^.]{0,100}\bassign\w*|\bassign\w*[^.]{0,80}without (?:the )?(?:prior )?(?:express )?(?:written )?consent|\bnon-?assignable\b/gi,
      'If the business or its assets are sold, a contract like this may need the other side\'s consent to move with it.',
      'List the contracts that restrict assignment and what consent each needs for this deal.', false),
    R('leg-restrictive', 'legal', 'medium', 'Exclusivity or restrictive covenants',
      /(?<![\w-])(?<!non[- ])(?<!non)exclusive(?!ly)(?! (?:jurisdiction|remedy|remedies|property|venue|benefit|forum|and irrevocable)\b)|\bexclusivity\b|sole and exclusive (?:supplier|provider|distributor|manufacturer|source)|\bnon-?compet\w+|(?:shall not|will not|agrees? not to|may not)[^.]{0,120}\b(?:compete|competing|competitor|competitive)\b|non-?solicit\w*|(?:shall not|will not|agrees? not to|neither party (?:shall|will|may))[^.]{0,100}(?:solicit|hire|recruit|employ)[^.]{0,80}(?:employee|customer|personnel|client)|most[- ]favou?red|\bMFN\b|no less favou?rable|right of first (?:refusal|offer|negotiation)/gi,
      'These limit what the business can do next, and may bind the buyer after a deal.',
      'Ask for a schedule of restrictive terms, their scope, territory and duration.', false),
    R('leg-autorenew', 'legal', 'medium', 'Automatic renewal',
      /auto(?:matic(?:ally)?)?[- ]renew\w*|evergreen|renews? automatically|automatically (?:renew|extend)/gi,
      'Commitments can roll over unnoticed, and notice windows are easy to miss.',
      'List renewal dates and notice deadlines for each contract.'),
    R('leg-unlimited', 'legal', 'high', 'Uncapped liability or exceptions to the liability cap',
      /unlimited liability|\buncapped\b|without (?:any )?(?:limit|limitation|cap)s? (?:on|of|as to) (?:its |their |the |any )?(?:liability|damages|losses)|no (?:limit|cap) on (?:liability|damages)|liability[^.\n]{0,40}(?:shall not be|is not|are not) (?:limited|capped)|liability[^.\n]{0,40}(?:is|are|shall be|will be) unlimited|(?:except|excluding|other than|notwithstanding)[^.]{0,200}(?:gross negligence|willful misconduct|wilful misconduct|fraud|indemnif\w+|breach of (?:its )?confidential\w+)[^.]{0,250}(?:liab\w+|damages)|(?:liab\w+|damages)[^.]{0,200}shall not apply to[^.]{0,200}(?:indemnif\w+|confidential\w*|negligence|misconduct)/gi,
      'Where liability is not capped, or the cap has exceptions, a single claim could exceed what the contract is worth.',
      'Ask for the liability cap in each material contract, its exceptions, and any insurance that backs uncapped terms.', false),
    R('leg-indemnity', 'legal', 'medium', 'Indemnities',
      /indemnif(?:y|ies|ication)|hold harmless/gi,
      'An indemnity moves risk onto you, often without a cap or time limit.',
      'Ask who gives each indemnity, to whom, and whether it is capped.'),
    R('leg-unilateral', 'legal', 'medium', 'One side can change terms or decide alone',
      /(?:sole|absolute) discretion|may (?:amend|modify|change|vary) (?:this agreement|these terms|the (?:fees|prices?))[^.\n]{0,30}(?:at any time|unilaterally|from time to time)|unilateral(?:ly)?/gi,
      'Terms that one party can rewrite are not firm terms.',
      'Ask which terms can be changed without consent and whether they ever have been.', false),
    R('leg-penalty', 'legal', 'medium', 'Liquidated damages and minimum commitments',
      /liquidated damages|early termination (?:fee|charge)s?|take[- ]or[- ]pay|minimum (?:purchase|commitment|order|quantity|charge|payment|fee|annual|royalt\w+|volume|spend|requirement|guarantee|of \$)/gi,
      'These create costs that do not depend on how the business performs.',
      'Request a schedule of minimum commitments and exit costs.'),
    R('leg-guarantee', 'legal', 'high', 'Guarantees and security',
      /personal guarant\w+|joint(?:ly)? and several(?:ly)?|parent (?:company )?guarantee|cross[- ]default|security interest|(?:charge|lien|pledge) over/gi,
      'Assets or individuals are on the hook beyond the contract itself.',
      'Request a register of all guarantees and security granted, and who benefits.'),
    R('leg-litigation', 'legal', 'high', 'Disputes and claims',
      /litigation|lawsuits?|legal proceedings?|class action|arbitration (?:claim|proceeding|demand)|subpoena|injunction|cease and desist|settlement agreement|pending claims?|demand letter|summons|statement of claim/gi,
      'Disputes cost money and attention, and can reveal problems the paperwork does not.',
      'Request a schedule of pending, threatened and settled claims for five years with exposure and insurer position.'),
    R('leg-ip', 'legal', 'high', 'Intellectual property assigned, shared or unclear',
      /jointly owned|co[- ]owned|licensed[- ]in|licen[cs]e[- ]back|(?:IP|intellectual property) (?:is|are) (?:owned|held) by (?:the founder|a director|an affiliate|a third)|contractors?[^.\n]{0,40}(?:retain|own)s? (?:the )?(?:IP|copyright|rights)|(?:invention|IP) assignments?[^.\n]{0,30}(?:missing|not (?:signed|in place))|(?:hereby )?assigns?[^.]{0,100}(?:right,? title,? and interest|all rights|intellectual property|inventions?)|works? made for hire|jointly own\w*|co-?own\w*|joint(?:ly)? (?:owner|ownership)/gi,
      'If the business does not clearly own what it sells, its value is in question.',
      'Request assignment agreements for all founders, employees and contractors who built core IP.'),
    R('leg-oral', 'legal', 'high', 'Verbal or undocumented agreements',
      /oral agreements?|verbal(?:ly)? (?:agreed|agreement|commitment|promise)|gentleman'?s agreement|no written (?:agreement|contract)|not (?:been )?(?:formally )?documented|unwritten/gi,
      'If it is not written down it is hard to enforce and easy to dispute.',
      'Ask for the terms in writing and confirmation from the counterparty.', false),
    R('leg-nonbinding', 'legal', 'medium', 'Not legally binding yet',
      /non[- ]binding|letter of intent|\bLOI\b|memorandum of understanding|\bMOU\b|heads of terms|agreement to agree/gi,
      'Intentions are not commitments. Either side can walk away.',
      'Confirm what is signed and binding versus what is still intent.', false),

    /* ---- compliance and regulatory ---- */
    R('cmp-action', 'compliance', 'critical', 'Regulator action or penalty',
      /notice of violation|consent order|warning letter|enforcement action|regulatory (?:action|investigation|inquiry|enquiry)|(?:was|were|been) fined|fine of|penalt(?:y|ies) of|sanctions? imposed|debarr\w+|licen[cs]e (?:revoked|suspended)|show[- ]cause|wells notice|civil investigative demand/gi,
      'A regulator has already acted, or is looking. Fines are often the smaller cost.',
      'Request all regulator correspondence for five years and the status of any open matter.'),
    R('cmp-noncompliance', 'compliance', 'high', 'Non-compliance or open audit findings',
      /non[- ]?complian\w+|not (?:fully )?compliant|failed to comply|in breach of (?:the )?(?:regulations?|law|act|directive|rules)|violat(?:ed|ion|ions) of|out of compliance|compliance gaps?|audit findings?|remediation plan/gi,
      'The business is saying, in its own words, that it falls short.',
      'Ask for the finding list, owners, deadlines and evidence of closure.'),
    R('cmp-missing-control', 'compliance', 'medium', 'Control missing or only planned',
      /no (?:formal |written )?(?:polic(?:y|ies)|procedures?|programmes?|programs?)|not yet (?:implemented|in place|adopted)|(?:in )?progress(?: toward)? (?:SOC ?2|ISO ?27001|certification)|planned (?:for|in) 20\d\d/gi,
      'A control that is planned is not a control. Check what happens in the meantime.',
      'Ask for the policy itself, who owns it and when it was last tested.', false),
    R('cmp-financial-crime', 'compliance', 'medium', 'Sanctions, bribery or money-laundering topics raised',
      /\bOFAC\b|politically exposed|\bPEPs?\b|money laundering|facilitation payments?|bribe\w*|kickbacks?|\bFCPA\b|UK Bribery Act|improper payments?|sanctions? (?:list|screening|exposure)/gi,
      'These are areas where a single incident can be deal-ending and personal liability is possible.',
      'Ask for the anti-corruption and sanctions policies, third-party due-diligence records and any incident log.'),
    R('cmp-privacy', 'compliance', 'high', 'Privacy or data-protection problem',
      /data (?:breach|leak|incident|exposure)|no (?:lawful basis|DPA|data processing agreement)|GDPR[^.\n]{0,40}(?:fine|breach|violation|complaint)|HIPAA[^.\n]{0,40}(?:breach|violation|complaint)|unencrypted (?:PHI|personal)|without (?:user )?consent/gi,
      'Privacy failures bring fines, notification duties and loss of customer trust.',
      'Request the breach log, records of processing and any regulator or complainant correspondence.'),
    R('cmp-licence', 'compliance', 'high', 'Licence, permit or certification problem',
      /(?:licen[cs]e|permit|certificat(?:e|ion)|accreditation|registration)[^.\n]{0,40}(?:lapsed|expired|revoked|suspended|pending|not (?:yet )?(?:obtained|held|renewed)|application)/gi,
      'Operating without a required licence can stop the business or void contracts.',
      'Request copies of every licence and permit with current validity dates.'),
    R('cmp-whistle', 'compliance', 'high', 'Whistleblower or internal investigation',
      /whistle-?blow\w*|retaliat\w+|internal investigation|forensic (?:review|investigation)|hotline (?:report|complaint)/gi,
      'Insiders or the company itself have already raised concerns serious enough to investigate.',
      'Ask for the investigation scope, findings and any remediation.'),
    R('cmp-product', 'compliance', 'medium', 'Product safety or recall signals',
      /product recall|\brecalled?\b|FDA (?:483|warning)|clinical hold|adverse events?|safety (?:incident|notice)|non[- ]conformit(?:y|ies)/gi,
      'Safety issues create liability that outlasts the product.',
      'Request the complaint, recall and corrective-action logs.'),

    /* ---- technology and security ---- */
    R('tech-incident', 'tech', 'critical', 'Past security incident',
      /ransomware|(?:security|cyber|data) (?:breach|incident|compromise)|(?:was|were|been) (?:hacked|breached|compromised)|unauthori[sz]ed access|exfiltrat\w+|phishing (?:attack|incident)|DDoS/gi,
      'An incident shows what has already gone wrong, and often what still can.',
      'Request incident reports, root-cause findings, notification records and insurer correspondence.'),
    R('tech-controls', 'tech', 'high', 'Basic security control missing',
      /no (?:MFA|2FA|multi[- ]factor|two[- ]factor)|shared (?:password|credential|account|login)s?|default (?:password|credentials)|plain ?text passwords?|hard[- ]?coded (?:password|credential|key|secret)s?|unencrypted|not encrypted|no (?:encryption|backups?|disaster recovery|DR plan|incident response|penetration test|pen ?test|logging|monitoring)|unpatched|(?:critical|high)[- ]severity (?:vulnerabilit\w+|findings?)|open (?:critical|high) (?:findings?|vulnerabilit\w+)/gi,
      'These are the gaps attackers look for first.',
      'Ask for the latest penetration test, vulnerability scan and a dated remediation plan.', false),
    R('tech-eol', 'tech', 'high', 'Unsupported or end-of-life technology',
      /end[- ]of[- ]life|\bEOL\b|end of (?:vendor )?support|no longer (?:supported|maintained|patched)|unsupported (?:version|software|OS)|out[- ]of[- ]support|legacy (?:system|platform|stack|code)|Windows (?:XP|7|Server 2008|Server 2012)|Python 2|CentOS 6/gi,
      'Unsupported software gets no security fixes and is costly to replace.',
      'Ask for an inventory of systems with support end dates and the replacement budget.', false),
    R('tech-copyleft', 'tech', 'high', 'Open-source licence risk',
      /\bAGPL\b|\bGPL(?:v?[23])?\b|copyleft|\bSSPL\b|no (?:SBOM|software bill of materials|open[- ]source (?:audit|policy))|open[- ]source[^.\n]{0,60}(?:not (?:reviewed|audited|tracked)|unvetted)/g,
      'Some licences can force you to release your own code or restrict commercial use.',
      'Request a software bill of materials and the results of an open-source licence scan.', false),
    R('tech-keyperson', 'tech', 'high', 'Knowledge held by one person',
      /(?:single|sole) (?:engineer|developer|person|point of failure)|only (?:one|\w+) (?:person|engineer|developer) (?:who|that|knows|understands|can)|key[- ]person (?:risk|dependency)|bus factor|tribal knowledge|undocumented (?:code|system|process)|no (?:documentation|runbook)/gi,
      'If that person leaves, so does the ability to run or change the system.',
      'Ask for documentation, access arrangements and a succession plan for critical systems.', false),
    R('tech-debt', 'tech', 'medium', 'Technical debt or reliability issues',
      /technical debt|tech debt|re-?platform\w*|monolith(?:ic)?|scalability (?:issue|limit|concern)s?|performance (?:issue|degradation)s?|outages?|downtime|SLA (?:breach|miss)\w*/gi,
      'Hidden engineering debt becomes a bill after the deal.',
      'Ask for uptime history, incident postmortems and the engineering backlog with estimates.'),
    R('tech-outsourced', 'tech', 'medium', 'Outsourced engineering',
      /outsourc\w+ (?:development|engineering|dev)|offshore (?:team|development)|built by (?:a |an )?(?:third[- ]party|vendor|agency|freelanc\w+)/gi,
      'Check who owns the code and whether you can get the source and the people.',
      'Request the development contracts, IP assignment clauses and escrow terms.'),

    /* ---- people and HR ---- */
    R('ppl-cic', 'people', 'high', 'Retention, severance or change-of-control payments',
      /retention bonus\w*|change[- ]in[- ]control (?:payment|bonus|benefit)s?|golden parachute|accelerated vesting|single[- ]trigger|severance (?:of|equal)|termination (?:payment|pay) of/gi,
      'These add cost at closing and can reward people for leaving.',
      'Request a schedule of all deal-triggered payments with amounts and recipients.'),
    R('ppl-claims', 'people', 'high', 'Employment claims or labour disputes',
      /harass\w+|discriminat\w+|wrongful (?:termination|dismissal)|grievances?|unfair dismissal|employment tribunal|labou?r (?:dispute|board|claim)|\bEEOC\b|collective bargaining|work stoppage|strikes?\b|unioni[sz]\w+/gi,
      'Open claims show culture and carry cost and reputational risk.',
      'Request a log of employment claims, complaints and settlements for five years.'),
    R('ppl-classification', 'people', 'high', 'Worker classification or pay compliance',
      /misclassif\w+|independent contractors?[^.\n]{0,60}(?:full[- ]time|employees?)|unpaid (?:overtime|wages|leave|holiday)|minimum wage|wage (?:claim|violation)s?|off[- ]the[- ]books|cash (?:payments?|wages) to (?:staff|workers)|undocumented workers?|(?:work (?:permit|visa)|I-?9)[^.\n]{0,40}(?:expired|lapsed|missing|not)/gi,
      "Back pay, penalties and tax can be large and are usually the buyer's problem.",
      'Ask for contractor lists with hours and duties, and right-to-work records.'),
    R('ppl-leadership', 'people', 'high', 'Key person leaving or leadership gap',
      /(?:founder|CEO|CTO|CFO|managing director|key (?:employee|executive|person))[^.\n]{0,60}(?:resign\w*|leav\w+|depart\w*|retir\w+|exit\w*|step(?:ping)? down)|(?:vacan(?:t|cy)|unfilled|interim|acting) (?:CEO|CFO|CTO|COO|head)|leadership (?:churn|turnover)|dependent on (?:the )?(?:founder|CEO|one person)/gi,
      'Value often rides on a few people. Their plans matter as much as the numbers.',
      'Ask for key-person retention terms and a leadership succession plan.'),
    R('ppl-pension', 'people', 'medium', 'Pension or long-term benefit obligations',
      /defined[- ]benefit|pension (?:deficit|shortfall|liabilit\w+)|unfunded|underfunded|multi[- ]employer|post[- ]retirement/gi,
      'These liabilities are long-dated and can swing a lot.',
      'Request the latest actuarial valuation and funding plan.'),
    R('ppl-restructuring', 'people', 'medium', 'Restructuring or layoffs',
      /redundanc(?:y|ies)|layoffs?|reduction in force|\bRIF\b|WARN Act|headcount reduction|hiring freeze|restructur\w+/gi,
      'Cuts affect delivery capacity and carry notice and severance duties.',
      'Ask for what was cut, what is planned and the cost and notice obligations.'),

    /* ---- property and environment ---- */
    R('prop-title', 'property', 'high', 'Title, lien or encumbrance issue',
      /(?:title|ownership)[^.\n]{0,40}(?:defects?|disputed?|cloud|unclear|contested|not (?:registered|transferred))|encumbranc\w+|\bliens?\b|easements?|right of way|restrictive covenants?|mortgages?|boundary dispute|adverse possession|unregistered/gi,
      'You may not get clean, unrestricted ownership of what you are paying for.',
      'Request title reports and a register of charges and third-party rights.'),
    R('prop-env', 'property', 'critical', 'Contamination or environmental liability',
      /contaminat\w+|asbestos|\bPFAS\b|underground storage tanks?|\bUSTs?\b|phase (?:II|2) (?:environmental|ESA|assessment)|recogni[sz]ed environmental condition|hazardous (?:waste|substances?|materials?)|remediation|brownfield|lead paint|\bmou?ld\b|radon|groundwater|superfund|environmental (?:liabilit\w+|claims?|notices?)/gi,
      'Environmental liability usually follows the owner or operator, whoever caused it.',
      'Request environmental assessments, remediation estimates and regulator correspondence.'),
    R('prop-zoning', 'property', 'high', 'Zoning, planning or permit gap',
      /non[- ]?conforming (?:use|building)|zoning (?:violations?|variance|breach|issues?)|planning permission[^.\n]{0,40}(?:not|without|refused|lacking|retrospective)|building (?:code|regulations?)[^.\n]{0,20}(?:violation|breach)|certificate of occupancy[^.\n]{0,30}(?:missing|not|expired|temporary)|unpermitted|without (?:permit|planning|building consent)|illegal (?:extension|conversion)|enforcement notice/gi,
      'Use or construction without approval can be ordered stopped or undone.',
      'Request planning consents, completion certificates and any enforcement correspondence.'),
    R('prop-physical', 'property', 'medium', 'Physical condition concerns',
      /deferred maintenance|structural (?:defect|issue|crack|damage|movement)s?|subsidence|water ingress|roof[^.\n]{0,30}(?:end of life|leak|replace\w*)|capex backlog|dilapidations?|flood(?:ing)?[ -](?:zone|risk|plain)|cladding|\bEWS1\b/gi,
      'Condition problems become capital spend you did not price in.',
      'Request a building survey and a capital expenditure plan.'),
    R('prop-lease', 'property', 'medium', 'Lease terms that could bite',
      /lease (?:expir\w+|ends?|terminat\w+)[^.\n]{0,40}(?:within|in|by)|rent (?:review|escalat\w+|increases?)|upward[- ]only|break (?:clause|option)|holdover|landlord'?s? consent|rent arrears|service charge (?:arrears|dispute)/gi,
      'Occupancy cost and security of tenure drive the value of a site-based business.',
      'Request all leases with a schedule of expiries, breaks, reviews and consents.'),

    /* ---- tax ---- */
    R('tax-dispute', 'tax', 'high', 'Tax audit, assessment or arrears',
      /tax (?:audits?|assessments?|investigations?|disputes?|inquir(?:y|ies)|enquir(?:y|ies)|notices?)|back[- ]tax|unpaid tax\w*|tax (?:arrears|liabilit(?:y|ies))|\bHMRC\b|tax liens?|deficiency notice|tax tribunal|penalt(?:y|ies) and interest|late filings?/gi,
      'Tax exposure travels with the company and grows with interest and penalties.',
      'Request tax authority correspondence, open-year schedules and filing compliance records.'),
    R('tax-tp', 'tax', 'medium', 'Transfer pricing or cross-border structure',
      /transfer pric\w+|permanent establishment|\bnexus\b|intercompany (?:loans?|charges?|royalt\w+|recharges?)|management fees? to|royalt(?:y|ies) to (?:affiliate|parent|group)|thin capitali[sz]ation|\bBEPS\b|treaty (?:shopping|benefit)s?/gi,
      'Internal pricing between countries is a frequent audit target.',
      'Request transfer pricing documentation and intercompany agreements.'),
    R('tax-position', 'tax', 'medium', 'Tax positions that may be challenged',
      /uncertain tax positions?|tax (?:shelters?|havens?|holidays?|avoidance)|offshore|aggressive (?:tax|position)s?|(?:section|§)\s?382|net operating loss(?:es)?|\bNOLs?\b[^.\n]{0,40}(?:limit\w*|expir\w+|restrict\w*)|R&D (?:tax )?credits?[^.\n]{0,40}(?:claim\w*|risk|challeng\w+)/gi,
      'Tax assets and benefits may not survive a change of ownership or a challenge.',
      'Ask for the basis of each material position and whether any have been reviewed by the authority.'),
    R('tax-indirect', 'tax', 'medium', 'Sales tax, VAT or payroll-tax gap',
      /(?:VAT|GST|sales tax|use tax|payroll tax|withholding tax|excise)[^.\n]{0,50}(?:unpaid|not (?:registered|collected|remitted|charged|filed)|exposure|owed|late|outstanding|dispute|error)|not registered for (?:VAT|GST)/gi,
      'Collected-but-unpaid or never-collected indirect tax is a direct liability.',
      'Request indirect-tax filings and a registration map by jurisdiction.'),

    /* ---- customers and market ---- */
    R('com-pipeline', 'commercial', 'high', 'Pipeline, pilots or intent counted as revenue',
      /(?:ARR|revenue|bookings|contracted)[^.\n]{0,60}(?:includes?|including|counts?|counted)[^.\n]{0,40}(?:pilots?|POCs?|proof of concept|trials?|LOIs?|letters? of intent|verbal|non[- ]binding|pipeline|unsigned)/gi,
      'Unsigned or unpaid business is not revenue and may never convert.',
      'Ask for a reconciliation of reported recurring revenue to signed, billable contracts.', false),
    R('com-lost', 'commercial', 'high', 'Lost or cancelling customers',
      /(?:lost|loss of|terminated|cancel+ed|non[- ]renew\w*|did not renew|gave notice|notice of (?:termination|non[- ]renewal)|moved to (?:a )?competitor|switch(?:ed|ing) to)[^.\n]{0,50}(?:customers?|clients?|contracts?|accounts?|distributors?|partners?)|(?:customers?|clients?|contracts?|accounts?)[^.\n]{0,40}(?:lost|cancel+ed|terminated|churned|non[- ]renew\w*|gave notice)/gi,
      'Customer losses are the earliest sign that the forecast is wrong.',
      'Request a list of customers lost or at risk in the last 24 months with reasons.'),
    R('com-pricing', 'commercial', 'medium', 'Discounting or pricing pressure',
      /(?:discounts?|rebates?|price (?:cuts?|concessions?|reductions?)|credit notes?|refunds?|free (?:months?|of charge))[^.\n]{0,50}(?:to (?:win|retain|close|secure)|increas\w+|growing|(?:\d{1,2})\s?%)|heavy discount\w*|deep discount\w*|price war|pricing pressure/gi,
      'Rising discounts hide weak demand behind headline revenue.',
      'Ask for price-realisation data: list price versus invoiced price by cohort.'),
    R('com-competition', 'commercial', 'medium', 'Competitive or market pressure',
      /(?:new )?(?:entrants?|competitors?)[^.\n]{0,40}(?:gaining|taking|undercut\w*|launch\w*|aggressive)|market share (?:loss|declin\w+|erosion|fell|dropp\w+)|commoditi[sz]\w+|substitute (?:product|technology)s?|declining (?:market|demand)|shrinking market/gi,
      'The market the numbers were built on may be moving.',
      'Ask for win/loss data and independent market-size evidence.'),
    R('com-forecast', 'commercial', 'medium', 'Stretched forecast assumptions',
      /hockey[- ]stick|aggressive (?:forecast|growth|assumption)s?|unrealistic|stretch targets?|not yet (?:achieved|signed|contracted)|assum\w+[^.\n]{0,30}\d{2,3}\s?%\s?(?:growth|CAGR|year[- ]on[- ]year|YoY)|\d{2,3}\s?%\s?(?:CAGR|growth)[^.\n]{0,40}(?:assum\w+|forecast|projected|plan)/gi,
      'Forecasts that need things to go right are not a basis for price.',
      'Ask for historical forecast accuracy and the assumptions behind each growth driver.'),

    /* ---- suppliers and ESG ---- */
    R('sup-single', 'supply', 'high', 'Single-source or concentrated supply',
      /sole[- ]source|single[- ]source|single supplier|one supplier|only (?:one|a single) (?:supplier|vendor|manufacturer|source)|no (?:alternative|second) (?:supplier|source)|dependen\w+ (?:on|upon) (?:a )?(?:single|one|sole)/gi,
      'If that supplier fails, raises prices or exits, the business has no fallback.',
      'Ask for supplier concentration data, contract terms and qualified alternates.', false),
    R('sup-contract', 'supply', 'medium', 'Weak supplier contracts',
      /no (?:written |long[- ]term )?(?:supply )?(?:agreements?|contracts?|SLAs?)|purchase orders? only|spot (?:market|purchas\w+)|no (?:right|ability) to audit|no audit rights?|sub-?contract\w*[^.\n]{0,40}(?:without|unapproved|undisclosed|unknown)/gi,
      "Without terms, prices, supply and quality are at the supplier's discretion.",
      'Request top-supplier contracts and audit rights.', false),
    R('sup-labour', 'supply', 'critical', 'Labour or safety abuse in the supply chain',
      /forced labou?r|child labou?r|modern slavery|human trafficking|debt bondage|unsafe (?:working )?conditions|sweatshop|\bXinjiang\b|\bUFLPA\b|(?:worker|labou?r) (?:abuse|exploitation)|fatalit\w+|\bLTIFR\b|serious injur\w+/gi,
      'Beyond the ethics, this can mean import bans, contract loss and director liability.',
      'Request supplier audit reports, corrective actions and the traceability map.'),
    R('sup-green', 'supply', 'medium', 'Environmental claims without proof',
      /(?:carbon[- ]neutral|net[- ]zero|climate[- ]positive|100\s?% (?:renewable|green|sustainable)|eco[- ]friendly|zero[- ]emission)[^.\n]{0,60}(?:by 20\d\d|claims?|targets?|pledges?|offsets?)|offsets? (?:only|alone)|no (?:baseline|verified|third[- ]party)[^.\n]{0,40}(?:emissions?|carbon|scope)|greenwash\w*/gi,
      'Unverified environmental claims are now an enforcement and litigation target.',
      'Ask for the emissions baseline, methodology and any third-party verification.', false),
    R('sup-emissions', 'supply', 'medium', 'Emissions, spill or environmental penalty',
      /emissions?[^.\n]{0,40}(?:exceed\w*|above|breach\w*|increas\w+|over (?:the )?limit|permit limit)|(?:exceed\w*|breach\w*)[^.\n]{0,30}(?:emission|discharge|effluent) (?:limit|permit|standard)s?|(?:spill|leak|discharge|effluent|pollution) (?:incident|event|into)|environmental (?:fine|penalt\w+|violation)s?/gi,
      'Operating limits that are being exceeded point to remediation and enforcement cost.',
      'Request monitoring data, permits and notices of violation.'),
    R('sup-geo', 'supply', 'medium', 'Trade or geographic exposure',
      /tariffs?|export controls?|embargo\w*|trade (?:restriction|war|ban)s?|customs (?:dispute|audit|duty)|port (?:congestion|closure)|(?:geo)?political (?:risk|instability)|conflict zone|single (?:country|region|port|plant|factory|site)/gi,
      'Concentration in one place turns local events into company-level events.',
      'Ask for revenue, supply and manufacturing by country and the mitigation plan.'),
  ];

  /* ------------------------------------------------------------------ */
  /* Text helpers                                                        */
  /* ------------------------------------------------------------------ */

  const NEGATION = /\b(?:no|not|without|nil|none|never|neither|nor|free of|absence of|absent|zero|unaware of|not aware of|didn['’]t|hasn['’]t|haven['’]t|isn['’]t|aren['’]t|wasn['’]t)\b[^.;\n]{0,30}$/i;

  function isNegated(text, start) {
    return NEGATION.test(text.slice(Math.max(0, start - 45), start));
  }

  function sentenceAround(text, start, end) {
    let s = start;
    let e = end;
    const lo = Math.max(0, start - 170);
    const hi = Math.min(text.length, end + 170);
    while (s > lo && !/[.!?\n]/.test(text[s - 1])) s -= 1;
    while (e < hi && !/[.!?\n]/.test(text[e])) e += 1;
    if (e < text.length && /[.!?]/.test(text[e])) e += 1;
    const raw = text.slice(s, e);
    const lead = raw.length - raw.trimStart().length;
    const out = raw.trim();
    return { text: out, hlStart: start - s - lead, hlEnd: end - s - lead };
  }

  function wordCount(text) {
    const m = text.match(/\S+/g);
    return m ? m.length : 0;
  }

  function fmt(n) {
    const digits = Number.isInteger(n) ? 0 : 2;
    return n.toLocaleString('en-US', { maximumFractionDigits: digits });
  }

  function dateLabel(d) {
    return d.toISOString().slice(0, 10);
  }

  /* ------------------------------------------------------------------ */
  /* Dates                                                               */
  /* ------------------------------------------------------------------ */

  const MONTHS = {
    january: 0, jan: 0, february: 1, feb: 1, march: 2, mar: 2, april: 3, apr: 3, may: 4, june: 5, jun: 5,
    july: 6, jul: 6, august: 7, aug: 7, september: 8, sep: 8, sept: 8, october: 9, oct: 9, november: 10, nov: 10,
    december: 11, dec: 11,
  };
  const MON = '(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)';

  function mkDate(y, m, d) {
    if (y < 1950 || y > 2100 || m < 0 || m > 11 || d < 1 || d > 31) return null;
    const dt = new Date(Date.UTC(y, m, d));
    return dt.getUTCMonth() === m ? dt : null;
  }

  function parseDates(text) {
    const found = [];
    const take = (re, build) => {
      let m;
      re.lastIndex = 0;
      while ((m = re.exec(text))) {
        const date = build(m);
        if (!date) continue;
        const start = m.index;
        const end = start + m[0].length;
        if (found.some((f) => start < f.end && end > f.start)) continue;
        found.push({ start, end, date });
      }
    };
    take(/\b(\d{4})-(\d{2})-(\d{2})\b/g, (m) => mkDate(+m[1], +m[2] - 1, +m[3]));
    take(new RegExp(`\\b(\\d{1,2})(?:st|nd|rd|th)?\\s+(?:of\\s+)?${MON}\\.?,?\\s+(\\d{4})\\b`, 'gi'),
      (m) => mkDate(+m[3], MONTHS[m[2].toLowerCase()], +m[1]));
    take(new RegExp(`\\b${MON}\\.?\\s+(\\d{1,2})(?:st|nd|rd|th)?,?\\s+(\\d{4})\\b`, 'gi'),
      (m) => mkDate(+m[3], MONTHS[m[1].toLowerCase()], +m[2]));
    take(/\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b/g, (m) => {
      const a = +m[1];
      const b = +m[2];
      return a > 12 ? mkDate(+m[3], b - 1, a) : mkDate(+m[3], a - 1, b);
    });
    take(new RegExp(`\\b${MON}\\.?,?\\s+(\\d{4})\\b`, 'gi'), (m) => {
      const month = MONTHS[m[1].toLowerCase()];
      return mkDate(+m[2], month + 1, 0) || mkDate(+m[2], month, 28);
    });
    return found.sort((a, b) => a.start - b.start);
  }

  /* ------------------------------------------------------------------ */
  /* Numbers                                                             */
  /* ------------------------------------------------------------------ */

  const NUM_TOKEN = /(?<![A-Za-z0-9_.])[-−]?[$€£]?\(?[-−]?\d[\d,]*(?:\.\d+)?\)?%?/g;

  function numbersInLine(line) {
    const out = [];
    let m;
    NUM_TOKEN.lastIndex = 0;
    while ((m = NUM_TOKEN.exec(line))) {
      const raw = m[0];
      const digits = raw.replace(/[^0-9.]/g, '');
      if (!digits || digits === '.') continue;
      const negative = (raw.includes('(') && raw.includes(')')) || /^[-−]|\(\s?[-−]/.test(raw);
      const value = parseFloat(digits.replace(/,/g, ''));
      if (Number.isNaN(value)) continue;
      const dec = (digits.split('.')[1] || '').length;
      out.push({
        value: negative ? -value : value,
        index: m.index,
        raw,
        dec,
        isPercent: raw.endsWith('%'),
        isYear: /^\d{4}$/.test(raw) && value >= 1900 && value <= 2100,
      });
    }
    return out;
  }

  function splitLines(text) {
    const lines = [];
    let pos = 0;
    for (const raw of text.split('\n')) {
      lines.push({ text: raw, start: pos, end: pos + raw.length });
      pos += raw.length + 1;
    }
    return lines;
  }

  /* ------------------------------------------------------------------ */
  /* Computed checks                                                     */
  /* ------------------------------------------------------------------ */

  function mkFinding(base) {
    return Object.assign({ spans: [], count: base.spans ? base.spans.length : 0, detail: '', quote: null }, base);
  }

  function quoteFor(docs, span) {
    if (!span) return null;
    return Object.assign({ doc: span.doc }, sentenceAround(docs[span.doc].text, span.start, span.end));
  }

  // Annual filings are full of "we could be subject to..." risk language. In those documents a match inside a
  // purely hypothetical sentence is not a statement that anything happened, so it is not raised as a finding.
  const FILING_FORM = /\bForm\s+10-K\b/i;
  const FILING_RISK_ITEM = /\bItem\s+1A\b/i;
  const HYPOTHETICAL = /\b(?:could|may|might|would|can|if|in the event|from time to time|potentially|possible|possibly|no assurances?|cannot (?:assure|provide any assurances?|guarantee)|can be no|there is a risk|risk that|in the future)\b/i;
  const FACTUAL = /\b(?:received|issued|filed|announced|identified|concluded|determined|commenced|settled|subpoena(?:ed|s)?|wells notice|restated|resigned|experienced|suffered|we (?:have|had) (?:been|received|identified|experienced|incurred))\b/i;

  // Facts about the company in a filing are written in the first person. A sentence with none of these is usually
  // describing a law, a regulator or the industry, not the company itself.
  const ABOUT_THE_COMPANY = /\b(?:we|our|us|ours|the company(?:['’]s)?|the registrant|management|the auditor|in our opinion)\b/i;

  const looksLikeFiling = (text) => FILING_FORM.test(text) && FILING_RISK_ITEM.test(text);
  const isHypothetical = (sentence) => HYPOTHETICAL.test(sentence) && !FACTUAL.test(sentence);
  const isAboutCompany = (sentence) => ABOUT_THE_COMPANY.test(sentence);

  // Rules that report something that happened, as opposed to a contract term. A term is definite on its own; an
  // event mentioned only in general words ("environmental matters", "litigation") is a weaker signal than one with a
  // date, an amount or a factual verb, so it is ranked one level lower and says so.
  const EVENT_RULES = new Set([
    'fin-going-concern', 'fin-audit-opinion', 'fin-restatement', 'fin-weakness', 'fin-covenant', 'fin-liquidity',
    'leg-litigation', 'cmp-action', 'cmp-noncompliance', 'cmp-privacy', 'cmp-licence', 'cmp-whistle', 'tech-incident',
    'ppl-claims', 'ppl-leadership', 'prop-title', 'prop-env', 'prop-zoning', 'tax-dispute', 'com-lost', 'sup-labour',
  ]);
  const SPECIFIC = /(?:19|20)\d{2}\b|[$€£]\s?\d|\b\d[\d,.]*\s?(?:million|billion|thousand|%|percent)\b|\b(?:received|issued|filed|announced|identified|concluded|determined|commenced|settled|paid|fined|ordered|found|breached|terminated|resigned|restated|experienced|suffered|notified|was|were|has been|have been|had been)\b/i;
  const DOWNGRADE = { critical: 'high', high: 'medium', medium: 'low', low: 'low' };

  function keywordFindings(docs, activeLenses, allSpans, tally) {
    const out = [];
    const maxSpans = allSpans ? Infinity : MAX_SPANS;
    const maxHits = allSpans ? Infinity : MAX_HITS_PER_DOC;
    for (const rule of RULES) {
      if (!activeLenses.has(rule.lens)) continue;
      const isEvent = EVENT_RULES.has(rule.id);
      const spans = [];
      const specificSpans = [];
      let count = 0;
      for (const doc of docs) {
        let hits = 0;
        rule.re.lastIndex = 0;
        let m;
        while ((m = rule.re.exec(doc.text)) && hits < maxHits) {
          if (m[0].length === 0) {
            rule.re.lastIndex += 1;
            continue;
          }
          if (rule.neg && isNegated(doc.text, m.index)) continue;
          if (doc.filing) {
            const sentence = sentenceAround(doc.text, m.index, m.index + m[0].length).text;
            if (isHypothetical(sentence)) {
              if (tally) tally.hypothetical += 1;
              continue;
            }
            if (!isAboutCompany(sentence)) {
              if (tally) tally.descriptive += 1;
              continue;
            }
          }
          hits += 1;
          count += 1;
          const span = { doc: doc.index, start: m.index, end: m.index + m[0].length };
          if (isEvent && SPECIFIC.test(sentenceAround(doc.text, span.start, span.end).text)) specificSpans.push(span);
          else if (spans.length < maxSpans) spans.push(span);
        }
      }
      if (!count) continue;
      // The most specific passages are cited first, so the quote shown is the strongest evidence.
      const ordered = [...specificSpans, ...spans].slice(0, maxSpans);
      const generalOnly = isEvent && specificSpans.length === 0;
      out.push(mkFinding({
        id: rule.id, lens: rule.lens, severity: generalOnly ? DOWNGRADE[rule.severity] : rule.severity,
        title: rule.title, why: rule.why, ask: rule.ask, spans: ordered, count, quote: quoteFor(docs, ordered[0]),
        basis: isEvent ? (generalOnly ? 'mentioned' : 'specific') : 'term',
        detail: generalOnly ? 'Mentioned only in general terms: no date, amount or specific event appears in the passage, so it is ranked one level lower.' : '',
      }));
    }
    return out;
  }

  function datesFindings(docs, today) {
    const expired = [];
    const expiring = [];
    const future = [];
    const order = [];
    const stale = [];
    const undated = [];
    const lead = /(expir\w*|valid (?:until|through|till|to)|renewal date|renews? on|lapses?|(?:end|expiry) date|good until|ends? on|certificate (?:end|valid))[^.\n]{0,25}$/i;
    const signedLead = /(signed|executed|dated|issued|prepared|date of (?:signature|execution))[^.\n]{0,25}$/i;
    // "X to Y" reads as a period. "X and Y" does too only after "between"; otherwise it is usually comparative
    // columns ("December 31, 2024 and 2023"), which legitimately run newest-first.
    const gapRe = /^\s*(?:to|until|till|through|[-–—])\s*$/i;
    const andRe = /^\s*and\s*$/i;

    for (const doc of docs) {
      const dates = parseDates(doc.text);
      dates.forEach((d, i) => {
        const before = doc.text.slice(Math.max(0, d.start - 60), d.start);
        const span = { doc: doc.index, start: d.start, end: d.end };
        if (lead.test(before)) {
          const days = (d.date - today) / DAY;
          if (days < 0) expired.push({ span, date: d.date });
          else if (days <= 90) expiring.push({ span, date: d.date });
        }
        if (signedLead.test(before) && (d.date - today) / DAY > 1) future.push({ span, date: d.date });
        const next = dates[i + 1];
        const gap = next ? doc.text.slice(d.end, next.start) : '';
        const isRange = next && (gapRe.test(gap) || (andRe.test(gap) && /between\s*$/i.test(before)));
        if (next && next.date < d.date && isRange) {
          order.push({ span: { doc: doc.index, start: d.start, end: next.end }, date: d.date, next: next.date });
        }
      });
      const past = dates.filter((d) => d.date <= today);
      if (past.length) {
        const latest = past.reduce((a, b) => (a.date > b.date ? a : b));
        if ((today - latest.date) / DAY > 548) {
          stale.push({ span: { doc: doc.index, start: latest.start, end: latest.end }, date: latest.date });
        }
      } else if (!dates.length && wordCount(doc.text) > 150) {
        undated.push(doc.name);
      }
    }

    const out = [];
    const build = (id, severity, title, why, ask, list, detailFn) => {
      if (!list.length) return;
      const spans = list.slice(0, MAX_SPANS).map((x) => x.span);
      out.push(mkFinding({
        id, lens: 'integrity', severity, title, why, ask, spans, count: list.length,
        detail: detailFn(list), quote: quoteFor(docs, spans[0]),
      }));
    };
    build('int-expired', 'high', 'Expired dates relied on as current',
      'Something the document treats as valid has already lapsed (certificate, licence, policy, price, term).',
      'Ask for the renewed or replacement document and confirm nothing was done in the gap.', expired,
      (l) => `Past expiry: ${l.slice(0, 5).map((x) => dateLabel(x.date)).join(', ')}.`);
    build('int-expiring', 'medium', 'Expires within 90 days',
      'It is valid today, but may not be by the time the deal closes or work is delivered.',
      'Ask what the renewal plan is and who owns it.', expiring,
      (l) => `Expiring: ${l.slice(0, 5).map((x) => dateLabel(x.date)).join(', ')}.`);
    build('int-future-dated', 'medium', 'Signed or issued on a future date',
      'A document cannot already be signed on a day that has not happened yet. It may be backdated, forward-dated or mistyped.',
      'Ask the author to confirm the real execution date.', future,
      (l) => `Dated: ${l.slice(0, 5).map((x) => dateLabel(x.date)).join(', ')}.`);
    build('int-date-order', 'high', 'End date comes before start date',
      'A period that runs backwards is a drafting or data error, and it is easy to miss.',
      'Ask which date is correct and whether other dates were copied from the same source.', order,
      (l) => `${dateLabel(l[0].date)} then ${dateLabel(l[0].next)}.`);
    build('int-stale', 'low', 'Latest date in the document is more than 18 months old',
      'Figures and statements may no longer be current.',
      'Ask for a current version or confirm in writing that nothing has changed.', stale,
      (l) => `Most recent date found: ${dateLabel(l[0].date)}.`);
    if (undated.length) {
      out.push(mkFinding({
        id: 'int-undated', lens: 'integrity', severity: 'low', title: 'No dates found',
        why: 'Without a date you cannot tell how current the document is or which version it is.',
        ask: 'Ask for the date prepared and the period it covers.', detail: `Checked: ${undated.join(', ')}.`,
      }));
    }
    return out;
  }

  function footingFindings(docs) {
    const out = [];
    let n = 0;
    for (const doc of docs) {
      let group = [];
      let subtotals = [];
      for (const line of splitLines(doc.text)) {
        const all = numbersInLine(line.text);
        const nums = all.filter((x) => !x.isYear);
        const label = (all[0] ? line.text.slice(0, all[0].index) : line.text).replace(/[^A-Za-z ]/g, ' ').trim();
        if (!nums.length) {
          if (line.text.trim() !== '') {
            group = [];
            subtotals = [];
          }
          continue;
        }
        // "Total average revenue per piece" or a row with a percent-change column is a comparison, not a sum.
        const isComparison = /\b(?:average|per|rate|ratio|margin|yield|percent|change)\b/i.test(label) || /%/.test(line.text);
        const isTotal = /^(?:grand\s+)?total\b|^sum\b|^net total\b/i.test(label) && !isComparison;
        const isSub = /^sub-?total\b/i.test(label);
        if (!isTotal && !isSub) {
          // Only short, label-then-number lines count as table rows. Prose sentences end the table.
          const rowLike = label.length >= 2 && label.split(/\s+/).length <= 6 && !/[.!?]["')]?\s*$/.test(line.text.trim());
          if (rowLike) {
            group.push({ nums });
          } else {
            group = [];
            subtotals = [];
          }
          continue;
        }
        const k = nums.length;
        const rows = group.filter((r) => r.nums.length === k);
        const candidates = [rows];
        if (isTotal && subtotals.length) candidates.push([...subtotals.filter((r) => r.nums.length === k), ...rows]);
        const usable = candidates.filter((c) => c.length >= 2);
        if (usable.length) {
          const bad = nums.map((tot, i) => {
            const attempts = usable.map((c) => {
              const sum = c.reduce((a, r) => a + r.nums[i].value, 0);
              const dec = Math.max(tot.dec, ...c.map((r) => r.nums[i].dec));
              const tol = Math.max(0.5 * Math.pow(10, -dec) * c.length, Math.abs(tot.value) * 0.001) + 1e-9;
              return { sum, diff: Math.abs(sum - tot.value), tol, total: tot.value };
            });
            return attempts.some((a) => a.diff <= a.tol) ? null : attempts[0];
          }).filter(Boolean);
          if (bad.length) {
            const w = bad[0];
            const rel = w.total ? w.diff / Math.abs(w.total) : 1;
            n += 1;
            const span = { doc: doc.index, start: line.start, end: line.end };
            out.push(mkFinding({
              id: `int-footing-${n}`, lens: 'integrity', severity: rel > 0.05 ? 'high' : 'medium',
              title: isSub ? "Subtotal doesn't match the rows above" : "Total doesn't match the rows above",
              why: 'Numbers that do not foot suggest a manual edit, a missing row or a figure pasted from another version.',
              ask: 'Ask for the working file and a reconciliation of this line to the underlying records.',
              spans: [span], count: 1, quote: quoteFor(docs, span),
              detail: `The rows above add up to ${fmt(w.sum)} but the stated total is ${fmt(w.total)} (difference ${fmt(w.diff)}).`,
            }));
          }
        }
        if (isSub) subtotals.push({ nums });
        group = [];
        if (isTotal) subtotals = [];
      }
    }
    return out;
  }

  const METRIC_WORDS = ['net income', 'net profit', 'operating income', 'operating profit', 'gross profit', 'gross margin', 'total assets', 'net debt', 'revenue', 'sales', 'turnover', 'profit', 'ebitda', 'cash', 'debt', 'headcount', 'employees', 'customers', 'arr', 'mrr', 'bookings', 'capex', 'backlog'];
  const CURRENT_STATE = new Set(['headcount', 'employees', 'customers', 'arr', 'mrr']);
  const PERIOD = /FY\s?'?\d{2,4}|(?:19|20)\d{2}|Q[1-4]\s?(?:FY)?\s?'?\d{2,4}|H[12]\s?\d{4}/i;

  // Prefer a period inside the match, then just after it, then just before it, never crossing a sentence.
  function periodNear(text, start, end) {
    const sentenceStart = Math.max(text.lastIndexOf('. ', start), text.lastIndexOf('\n', start)) + 1;
    const tail = text.slice(end, end + 30);
    const sentenceEnd = tail.search(/[.\n]\s/);
    const windows = [
      text.slice(start, end),
      sentenceEnd === -1 ? tail : tail.slice(0, sentenceEnd),
      text.slice(Math.max(sentenceStart, start - 40), start),
    ];
    for (const w of windows) {
      const p = w.match(PERIOD);
      if (p) return p[0].replace(/\s/g, '').toUpperCase();
    }
    return null;
  }

  function conflictFindings(docs) {
    const re = new RegExp(
      `\\b(${METRIC_WORDS.join('|')})\\b([^\\n.;]{0,40}?)(?:\\bof\\b|\\bwas\\b|\\bis\\b|\\bwere\\b|\\bat\\b|\\bto\\b|:|=|\\breached\\b|\\bstands? at\\b|\\btotall?ed\\b)\\s*(?:approximately |approx\\.? |about |circa |around )?([$€£]|USD |EUR |GBP )?\\s?(\\d[\\d,]*(?:\\.\\d+)?)\\s?(million|billion|thousand|bn|mm|[mMbBkK])?\\b`,
      'gi'
    );
    const groups = new Map();
    for (const doc of docs) {
      re.lastIndex = 0;
      let m;
      while ((m = re.exec(doc.text))) {
        const end = m.index + m[0].length;
        if (/^\s?%/.test(doc.text.slice(end, end + 2))) continue;
        const metric = m[1].toLowerCase();
        const scale = { million: 1e6, mm: 1e6, m: 1e6, billion: 1e9, bn: 1e9, b: 1e9, thousand: 1e3, k: 1e3 }[(m[5] || '').toLowerCase()] || 1;
        const value = parseFloat(m[4].replace(/,/g, '')) * scale;
        if (!Number.isFinite(value)) continue;
        const period = periodNear(doc.text, m.index, end);
        if (!period && !CURRENT_STATE.has(metric)) continue;
        const key = `${metric}|${period || 'now'}`;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push({ doc: doc.index, start: m.index, end, value, metric, period });
      }
    }
    const out = [];
    let n = 0;
    for (const items of groups.values()) {
      const sorted = [...items].sort((a, b) => a.value - b.value);
      const lo = sorted[0];
      const hi = sorted[sorted.length - 1];
      if (!lo.value || (hi.value - lo.value) / hi.value < 0.02) continue;
      n += 1;
      const spans = [lo, hi].map((x) => ({ doc: x.doc, start: x.start, end: x.end }));
      const rel = (hi.value - lo.value) / hi.value;
      const label = `${lo.metric}${lo.period ? ` for ${lo.period}` : ''}`;
      out.push(mkFinding({
        id: `int-conflict-${n}`, lens: 'integrity', severity: rel >= 0.1 ? 'high' : 'medium',
        title: `Different figures given for ${label}`,
        why: 'The same measure is stated two ways. One is wrong, or they cover different scope. Either way the reader is guessing.',
        ask: 'Ask which figure is correct, and for a reconciliation between them (period, scope, currency).',
        spans, count: items.length, quote: quoteFor(docs, spans[0]),
        detail: `${fmt(lo.value)} in ${docs[lo.doc].name} versus ${fmt(hi.value)} in ${docs[hi.doc].name}. Confirm they refer to the same period and scope.`,
      }));
    }
    return out;
  }

  function thresholdFindings(docs, activeLenses) {
    const out = [];
    const spansOf = (hits) => hits.map((h) => ({ doc: h.doc, start: h.start, end: h.end }));
    const add = (id, lens, severity, title, why, ask, hits, detail) => {
      if (!hits.length) return;
      const spans = spansOf(hits).slice(0, MAX_SPANS);
      out.push(mkFinding({ id, lens, severity, title, why, ask, spans, count: hits.length, detail, quote: quoteFor(docs, spans[0]) }));
    };
    const scan = (re, fn) => {
      const hits = [];
      for (const doc of docs) {
        re.lastIndex = 0;
        let m;
        while ((m = re.exec(doc.text))) {
          if (m[0].length === 0) { re.lastIndex += 1; continue; }
          const r = fn(m, doc);
          if (r) hits.push(Object.assign({ doc: doc.index, start: m.index, end: m.index + m[0].length }, r));
        }
      }
      return hits;
    };
    const top = (hits) => hits.reduce((a, b) => (RANK[a.severity] <= RANK[b.severity] ? a : b));

    if (activeLenses.has('commercial')) {
      const conc = scan(/(\d{1,3}(?:\.\d+)?)\s?%\s+of\s+(?:(?:our|its|the|total|annual|net|group|company|recurring)\s+)*(?:revenues?|sales|ARR|MRR|turnover|billings)/gi, (m, doc) => {
        const ctx = doc.text.slice(Math.max(0, m.index - 140), m.index + m[0].length + 80);
        if (!/customer|client|account|distributor|reseller|buyer|payer/i.test(ctx)) return null;
        const pct = parseFloat(m[1]);
        const topN = /top\s+(?:\d+|ten|five|three)|largest\s+(?:\d+|ten|five|three)/i.test(ctx);
        let severity = null;
        if (topN) severity = pct >= 70 ? 'high' : pct >= 50 ? 'medium' : null;
        else severity = pct >= 40 ? 'critical' : pct >= 25 ? 'high' : pct >= 15 ? 'medium' : null;
        return severity ? { severity, pct } : null;
      });
      if (conc.length) {
        const t = top(conc);
        add('com-concentration', 'commercial', t.severity, 'Heavy dependence on few customers',
          'Losing one account, or a price renegotiation, would move the whole business. Large single customers are also where related-party and side-deal risk hides.',
          'Request revenue by customer for three years, contract terms for the largest accounts and confirmation none is a related party.',
          conc, `Highest share stated: ${t.pct}%.`);
      }
      const churn = scan(/(?:(?:customer|logo|client|revenue|gross|annual|monthly)\s+)?churn(?:\s+rate)?[^.\n]{0,40}?(\d{1,2}(?:\.\d+)?)\s?%/gi, (m) => {
        const pct = parseFloat(m[1]);
        const monthly = /monthly/i.test(m[0]);
        const severity = monthly ? (pct >= 3 ? 'high' : null) : pct >= 15 ? 'high' : pct >= 8 ? 'medium' : null;
        return severity ? { severity, pct } : null;
      });
      if (churn.length) {
        const t = top(churn);
        add('com-churn', 'commercial', t.severity, 'High customer churn',
          'Churn at this level means the business has to keep refilling a leaking bucket just to stand still.',
          'Request cohort retention by customer size and the reasons customers left.', churn, `Churn stated: ${t.pct}%.`);
      }
      const nrr = scan(/(?:net (?:revenue|dollar) retention|\bNRR\b|\bNDR\b)[^.\n]{0,40}?(\d{2,3}(?:\.\d+)?)\s?%/gi, (m) => {
        const pct = parseFloat(m[1]);
        return pct < 100 ? { severity: 'high', pct } : null;
      });
      add('com-nrr', 'commercial', 'high', 'Net revenue retention below 100%',
        'The existing customer base shrinks even before churn is replaced by new sales.',
        'Request retention and expansion by cohort and the pricing and upsell history.', nrr,
        nrr.length ? `Stated: ${nrr[0].pct}%.` : '');
    }

    if (activeLenses.has('people')) {
      const attr = scan(/(?:attrition|(?:staff|employee|engineer|voluntary) turnover|turnover of (?:staff|employees))[^.\n]{0,40}?(\d{1,2}(?:\.\d+)?)\s?%/gi, (m, doc) => {
        const ctx = doc.text.slice(Math.max(0, m.index - 100), m.index + m[0].length + 60);
        if (!/staff|employee|headcount|people|workforce|engineer|voluntary|regretted|team/i.test(ctx)) return null;
        const pct = parseFloat(m[1]);
        const severity = pct >= 25 ? 'high' : pct >= 15 ? 'medium' : null;
        return severity ? { severity, pct } : null;
      });
      if (attr.length) {
        const t = top(attr);
        add('ppl-attrition', 'people', t.severity, 'High staff attrition',
          'Constant replacement costs money, loses knowledge and often signals a problem with management or pay.',
          'Request attrition by team and tenure, exit-interview themes and regretted-loss data.', attr, `Attrition stated: ${t.pct}%.`);
      }
    }
    return out;
  }

  function luhn(s) {
    let sum = 0;
    let alt = false;
    for (let i = s.length - 1; i >= 0; i -= 1) {
      let d = +s[i];
      if (alt) { d *= 2; if (d > 9) d -= 9; }
      sum += d;
      alt = !alt;
    }
    return sum % 10 === 0;
  }

  function sensitiveFindings(docs) {
    const out = [];
    const collect = (re, ok) => {
      const spans = [];
      let count = 0;
      for (const doc of docs) {
        re.lastIndex = 0;
        let m;
        while ((m = re.exec(doc.text))) {
          if (ok && !ok(m)) continue;
          count += 1;
          if (spans.length < MAX_SPANS) spans.push({ doc: doc.index, start: m.index, end: m.index + m[0].length });
        }
      }
      return { spans, count };
    };
    const push = (id, severity, title, why, ask, { spans, count }) => {
      if (!count) return;
      out.push(mkFinding({ id, lens: 'integrity', severity, title, why, ask, spans, count, quote: quoteFor(docs, spans[0]) }));
    };

    const masked = /^[*xX•<{$#]+$|redacted|\*{3,}|<[^>]+>|\$\{/i;
    const keys = collect(/-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----|\bAKIA[0-9A-Z]{16}\b|\b(?:password|passwd|pwd|secret|api[_ -]?key|access[_ -]?token|auth[_ -]?token)\b\s*[:=]\s*["']?[^\s"',;]{6,}/gi,
      (m) => !masked.test(m[0].split(/[:=]/).pop().trim()));
    push('int-secret', 'high', 'Credentials or secrets appear in the text',
      'Anyone with this file has the access it grants. It also shows how secrets are handled in practice.',
      'Treat the credential as compromised, ask for it to be rotated and ask how it ended up in a document.', keys);

    const ssn = collect(/\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b/g);
    const card = collect(/\b(?:\d[ -]?){13,19}\b/g, (m) => {
      const digits = m[0].replace(/\D/g, '');
      return digits.length >= 13 && digits.length <= 19 && /^[3-6]/.test(digits) && luhn(digits);
    });
    push('int-pii', 'high', 'Personal or payment identifiers in the document',
      'Sharing this data may breach privacy law, and the people it belongs to have not agreed to it being reviewed.',
      'Ask for a redacted copy and confirm how the data was shared and who has seen it.',
      { spans: [...ssn.spans, ...card.spans].slice(0, MAX_SPANS), count: ssn.count + card.count });

    const inj = collect(/ignore (?:all |any )?(?:previous|prior|above) (?:instructions|prompts)|disregard (?:the )?(?:above|previous) (?:instructions|text)|system prompt|<\/?system|override (?:the )?(?:instructions|rules)|do not (?:flag|report|mention) (?:this|any)/gi);
    if (inj.count) {
      out.push(mkFinding({
        id: 'int-injection', lens: 'integrity', severity: 'critical', title: 'Text that tries to instruct the reviewer',
        why: 'Someone planted instructions aimed at a person or an AI tool doing the review. That is deliberate and is a trust problem by itself.',
        ask: 'Escalate. Ask who authored and edited the file, and review every other document from the same source with extra care.',
        spans: inj.spans, count: inj.count, quote: quoteFor(docs, inj.spans[0]),
      }));
    }
    return out;
  }

  function currencyFindings(docs) {
    const symbols = { '$': /\$/g, '€': /€/g, '£': /£/g, '¥': /¥/g };
    const counts = {};
    const spans = [];
    for (const doc of docs) {
      for (const [sym, re] of Object.entries(symbols)) {
        re.lastIndex = 0;
        let m;
        while ((m = re.exec(doc.text))) {
          counts[sym] = (counts[sym] || 0) + 1;
          if (counts[sym] === 1) spans.push({ doc: doc.index, start: m.index, end: m.index + 1 });
        }
      }
    }
    const used = Object.keys(counts).filter((s) => counts[s] >= 3);
    const all = docs.map((d) => d.text).join('\n');
    if (used.length < 2 || /exchange rate|converted|translated at|fx rate|constant currency/i.test(all)) return [];
    return [mkFinding({
      id: 'int-currency', lens: 'integrity', severity: 'medium', title: 'Several currencies with no stated conversion',
      why: 'Mixed currencies make totals and comparisons unreliable and hide exchange-rate exposure.',
      ask: 'Ask for the reporting currency, the rates used and the date they were set.',
      spans: spans.slice(0, MAX_SPANS), count: used.length, detail: `Currencies seen: ${used.join(' ')}.`,
    })];
  }

  function missingProvisionFindings(docs) {
    const required = [
      ['governing law', /govern(?:ed|ing) (?:by )?(?:the )?law|laws of|jurisdiction/i],
      ['termination', /terminat|\bend(?:s|ed)? (?:this|the|it)\b|cancel|expir/i],
      ['a limit on liability', /limitation of liability|liability (?:shall|will) (?:not|be) (?:exceed|limited)|aggregate liability|cap(?:ped)? (?:on|at)/i],
      ['confidentiality', /confidential/i],
      ['signatures', /signed|signature|executed|in witness/i],
    ];
    const out = [];
    for (const doc of docs) {
      const head = doc.text.slice(0, 800);
      const looksLikeContract = /\b(agreement|contract|deed|lease|terms and conditions)\b/i.test(head) && /\b(parties|party|hereby|whereas|shall)\b/i.test(doc.text);
      if (!looksLikeContract || wordCount(doc.text) < 15) continue;
      const missing = required.filter(([, re]) => !re.test(doc.text)).map(([name]) => name);
      if (!missing.length) continue;
      out.push(mkFinding({
        id: `leg-missing${docs.length > 1 ? `-${doc.index + 1}` : ''}`, lens: 'legal', severity: missing.length >= 3 ? 'high' : 'medium',
        title: 'Standard provisions not found',
        why: 'This reads like a contract but is missing terms that most contracts include. It may be incomplete, a summary, or a template.',
        ask: 'Confirm you have the full executed agreement with all schedules and amendments.',
        detail: `${doc.name}: no mention of ${missing.join(', ')}.`,
      }));
    }
    return out;
  }

  function benfordFinding(docs) {
    const counts = new Array(10).fill(0);
    let n = 0;
    for (const doc of docs) {
      const stripped = doc.text.replace(/\b\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}\b/g, ' ');
      for (const line of stripped.split('\n')) {
        for (const x of numbersInLine(line)) {
          if (x.isPercent || x.isYear || Math.abs(x.value) < 10) continue;
          counts[+String(Math.abs(x.value))[0]] += 1;
          n += 1;
        }
      }
    }
    // The 0.015 threshold is a large-sample rule of thumb. Below a few hundred numbers it is mostly noise.
    if (n < 300) return [];
    let mad = 0;
    for (let d = 1; d <= 9; d += 1) mad += Math.abs(counts[d] / n - Math.log10(1 + 1 / d));
    mad /= 9;
    if (mad <= 0.015) return [];
    return [mkFinding({
      id: 'fin-benford', lens: 'financial', severity: 'medium', title: "Leading digits don't follow the natural pattern",
      why: 'In real ledgers, about 30% of numbers start with 1 and fewer than 5% with 9. Invented, rounded or manipulated figures often break that. This is a prompt to look closer, not proof.',
      ask: 'Run a full digit and duplicate test on the underlying ledger or invoice listing.',
      detail: `Across ${n} numbers the average deviation from the expected pattern is ${(mad * 100).toFixed(1)} points (above 1.5 is usually treated as non-conforming).`,
    })];
  }

  /* ------------------------------------------------------------------ */
  /* Entry point                                                         */
  /* ------------------------------------------------------------------ */

  function verdictFor(findings) {
    const counts = { critical: 0, high: 0, medium: 0, low: 0 };
    findings.forEach((f) => { counts[f.severity] += 1; });
    let level = 'none';
    if (counts.critical) level = 'critical';
    else if (counts.high >= 2 || counts.high + counts.medium >= 6) level = 'high';
    else if (counts.high || counts.medium) level = 'medium';
    else if (counts.low) level = 'low';
    const headline = {
      critical: 'Serious problems found. Do not rely on this yet.',
      high: 'Several issues need answers before you rely on this.',
      medium: 'A few points to confirm before you rely on this.',
      low: 'Only minor points. Worth a quick confirmation.',
      none: 'Nothing flagged by these checks.',
    }[level];
    return { level, headline, counts };
  }

  function analyze(documents, options) {
    const opts = options || {};
    const today = opts.today ? new Date(opts.today) : new Date();
    const chosen = Array.isArray(opts.lenses) ? opts.lenses : LENSES.map((l) => l.id);
    const active = new Set([...chosen, 'integrity']);
    const docs = (documents || []).map((d, i) => ({
      index: i,
      name: d.name || `Document ${i + 1}`,
      text: String(d.text || '').replace(/\r\n?/g, '\n'),
    }));
    docs.forEach((d) => { d.filing = looksLikeFiling(d.text); });
    const tally = { hypothetical: 0, descriptive: 0 };

    let findings = [];
    if (docs.length) {
      findings = [
        ...keywordFindings(docs, active, Boolean(opts.allSpans), tally),
        ...datesFindings(docs, today),
        ...footingFindings(docs),
        ...conflictFindings(docs),
        ...thresholdFindings(docs, active),
        ...sensitiveFindings(docs),
        ...currencyFindings(docs),
        ...(active.has('legal') ? missingProvisionFindings(docs) : []),
        ...(active.has('financial') ? benfordFinding(docs) : []),
      ];
    }
    findings = findings
      .map((f) => Object.assign({}, f, {
        lensName: LENS_NAME[f.lens],
        places: Array.from(new Set(f.spans.map((s) => docs[s.doc].name))).join(', '),
        firstPos: f.spans.length ? f.spans[0].start : Number.MAX_SAFE_INTEGER,
      }))
      .sort((a, b) => RANK[a.severity] - RANK[b.severity] || LENS_ORDER[a.lens] - LENS_ORDER[b.lens] || a.firstPos - b.firstPos);

    return {
      findings,
      verdict: verdictFor(findings),
      stats: {
        documents: docs.length,
        words: docs.reduce((a, d) => a + wordCount(d.text), 0),
        lenses: active.size,
        hypotheticalSuppressed: tally.hypothetical,
        descriptiveSuppressed: tally.descriptive,
        checks: RULES.filter((r) => active.has(r.lens)).length + 12,
      },
    };
  }

  return { analyze, verdictFor, sentenceAround, LENSES, INTEGRITY, SEVERITIES, LENS_NAME, LENS_ORDER, RANK, wordCount };
});
