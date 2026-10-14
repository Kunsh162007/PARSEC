# Fine Print and PARSEC: document due diligence with Nemotron on Nebius

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

Built for the Nebius x NVIDIA Global AI Hackathon. Two tools share one Python API and one set of model routes
(`config/models.yaml`):

| Tool | What it does | Where |
|---|---|---|
| **Fine Print** | Checks any document (contracts, accounts, policies, reports) across nine due diligence areas plus document integrity, and marks the clauses and claims worth asking about. The rules run **in the browser**. Optional server-side reads use Nemotron (an AI read, a clause pass, a "what this file still needs" list, a findings memo) and Tavily (a public-source check on named companies). | `/review/` |
| **PARSEC** | M&A quality-of-earnings pipeline: reads the Adjusted EBITDA reconciliation in a release or CIM, checks every line against the document and the arithmetic in code, questions the add-backs (against the company's own table and any ledger supplied), and flags customer concentration. Findings drawn from documents carry the verbatim quote, its offsets and the document's SHA-256. | `/parsec/` |

**Nothing in this project is simulated.** With no key, or if a service fails, a result says it was unavailable. The
numbers below come from scripts in this repository run on real data; each section names its command and result file.
Where a figure was not saved to a result file, the text says so.

---

## Quick start

Requirements: Python 3.11+, Node 20+ (only for the browser-rules tests and the CUAD rules benchmark), and two API keys.

```bash
git clone <this repository> && cd <this repository>
python -m venv .venv && source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

cp .env.example .env                                        # then add your keys (see below)
python scripts/check_env.py --live                          # confirms each key works, prints no secrets

uvicorn apps.api.main:app --port 8000
```

Then open <http://127.0.0.1:8000/review/> (Fine Print) or <http://127.0.0.1:8000/parsec/> (PARSEC). On Windows use
`127.0.0.1` rather than `localhost` if the page refuses to connect. "Or try real public documents" loads an SEC-filed
contract (CUAD, CC BY 4.0) and an SEC enforcement order (public domain) in one click. `DEMO.md` is a two-minute walkthrough.
Fine Print also works without the server: open `apps/review/index.html` in a browser. The rules run locally; the
example documents and all optional reads appear only when the page is served by the API.

Docker: `docker build -f infra/docker/Dockerfile -t parsec . && docker run --env-file .env -p 8000:8000 parsec`
(tested: a 264 MB image that runs as a non-root user, reports healthy, contains no `.env` or `data/`, and answers live
model calls with the keys passed by `--env-file`).

### Keys

| Variable | Needed for | Get one |
|---|---|---|
| `NEBIUS_API_KEY` | Fine Print AI read, clause pass, "what this file still needs" and memo; PARSEC reconciliation reading and committee questions | <https://studio.nebius.com> |
| `TAVILY_API_KEY` | Public-source check on named companies (Fine Print and PARSEC) | <https://app.tavily.com> |
| `REVIEW_API_TOKEN` | Optional access code for the paid Fine Print endpoints (`/api/review/deep`, `/requirements`, `/memo`) | any string you choose |

`REVIEW_RATE_PER_MINUTE` (default 6) and `REVIEW_DAILY_BUDGET_USD` (default 2.0) tune the guard on those endpoints;
see `.env.example` and `SECURITY.md`.

Without keys, Fine Print's rules still run. PARSEC still runs its code-only checks (data-room tracker, injection check,
customer concentration, and the EBITDA bridge for the one fixed layout it was first built for), but reading a real
company's reconciliation needs the Nebius key; without it the result lists that step as unavailable.

---

## Tests

```bash
python -m pytest -q         # 263 Python tests: gateway, AI read, clause pass, requirements, memo, pipeline, bridge, extractor, guard, web check, scoring
npm test                    # 113 tests: rules engine, connections layer, workflow deliverables
```

Most tests use small hand-written strings as inputs; the integration tests use FinanceBench and FinQA samples in
`backtest/`. They check behaviour and are not evidence of accuracy. Accuracy is measured separately, on real data, below.

---

## Measured results

### 1. Fine Print rules on CUAD: 510 real contracts, clauses labelled by lawyers

Dataset: [CUAD v1](https://www.atticusprojectai.org/cuad) (Hendrycks, Burns, Chen, Ball, NeurIPS 2021; CC BY 4.0;
DOI [10.5281/zenodo.4595826](https://doi.org/10.5281/zenodo.4595826)). 510 commercial contracts from SEC filings with
13,823 lawyer-labelled clause spans. All label offsets were verified against the text.

```bash
python -m benchmarks.cuad.dataset --fetch        # downloads 106 MB into data/cuad/ and verifies the checksum
python -m benchmarks.cuad.run --split test       # rules on the held-out half, free
python -m benchmarks.cuad.run --split test --ai 30   # plus the generic AI read on 30 contracts, about $0.50
```

**Protocol.** The contracts are split 50/50 by a fixed seed. Rules were examined and tuned on the **dev** half only. The
numbers below are from the **held-out test** half (255 contracts), run once after tuning
(`benchmarks/cuad/results.json`; before tuning: `benchmarks/cuad/history/00_untuned_test.json`).
Recall is "of the contracts where a lawyer labelled this clause, in how many did a finding overlap it?". Precision is "of
the places a rule fired, how many sit on a labelled clause?" and is a lower bound, because labellers did not mark every
clause. Highlighting text at random would have a precision of about 4.6%.

**Across the 15 clause categories Fine Print has rules for (934 labelled clauses), recall went from 27% to 58%.**

| Clause category | Contracts with it | Recall before tuning | Recall after |
|---|---|---|---|
| Anti-Assignment | 184 | 13% | 85% |
| Exclusivity | 99 | 78% | 73% |
| Renewal Term | 91 | 52% | 52% |
| Termination For Convenience | 91 | 13% | 69% |
| Minimum Commitment | 76 | 8% | 25% |
| IP Ownership Assignment | 62 | 0% | 66% |
| Uncapped Liability | 62 | 13% | 24% |
| Change Of Control | 59 | 46% | 46% |
| Non-Compete | 56 | 11% | 52% |
| Rofr/Rofo/Rofn | 43 | 49% | 49% |
| Liquidated Damages | 29 | 52% | 41% |
| No-Solicit Of Employees | 27 | 4% | 63% |
| Joint IP Ownership | 20 | 30% | 55% |
| No-Solicit Of Customers | 18 | 6% | 22% |
| Most Favored Nation | 17 | 6% | 18% |

| Rule | Precision before | Precision after | Notes |
|---|---|---|---|
| `leg-unlimited` | 1% (1,052 hits) | 38% (45 hits) | "including, without limitation" boilerplate was being flagged as uncapped liability |
| `leg-penalty` | 19% | 50% | bare "penalties" (as in "penalties and interest") removed |
| `leg-restrictive` | 17% | 27% | "non-exclusive", "exclusive jurisdiction" and "exclusive remedy" no longer match |
| `leg-ip` | 16% | 47% | |
| `leg-assignment` (new) | n/a | 78% | |
| `leg-tfc` | 45% (29 hits) | 24% (284 hits) | far more recall, at lower precision |
| `leg-coc` | 49% | 36% | no longer credited with Anti-Assignment clauses, which `leg-assignment` now owns |

What the numbers do not show well:

- Two categories lost recall in exchange for precision (Exclusivity 78% to 73%, Liquidated Damages 52% to 41%).
- Several categories are still weak: Minimum Commitment 25%, Uncapped Liability 24%, No-Solicit Of Customers 22%, MFN 18%.
- Fine Print has no rule for most of CUAD's other 26 categories (governing law, parties, dates, insurance, audit rights...).
- Rules were tuned on the dev half of the same dataset, so these figures are not independent of CUAD.

### 2. Model clause pass: thin the contract, then verify every hit against its passage

```bash
python -m benchmarks.cuad.verify_run --split dev --n 25     # where prompt and batch size were tuned
python -m benchmarks.cuad.verify_run --split test --n 30    # held out, run once
```

A deliberately broad net (`packages/review/clauses.py`) picks every passage that could be one of the 15 clause types,
across the whole contract. On the 25 dev contracts it kept 36% of passages (2,440 of 6,694) and those passages still
contained 98 of the 100 labelled clauses (75% to 100% by category); on the 30 test contracts, 34% of passages and 129 of
136. Nemotron Super then classifies only those numbered passages and must quote the exact words; a hit whose quote is not
inside the passage it names is discarded. Scoring is per category: a passage tagged "most favoured nation" counts only
if it overlaps a clause the lawyers labelled as one.

**Held-out test: 30 contracts, 136 labelled clauses, $0.49, 0 failed calls, 14 hits discarded by the citation check**
(`benchmarks/cuad/history/04_verify_test.json`).

| | Recall |
|---|---|
| Rules | 54% |
| Model, thin-then-verify | 77% |
| Either | **80%** |

Model precision within the correct category was 52% (a lower bound). The clause pass runs as part of the product's AI
read whenever the legal area is selected (it is selected by default). Still weak: uncapped liability (44% combined) and
minimum commitment (50%); MFN and no-solicit of customers had only one or two examples in the sample. During tuning a
real fault surfaced: with a 3,000-token limit the model spent the whole budget reasoning and returned no answer on about
1 call in 5. The gateway now reports an empty answer as a named failure and tries the fallback model, and this task has
an 8,000-token limit (`config/models.yaml`, `clause_verification_json`).

### 3. The generic AI read on the same contracts

The AI read also runs a second, generic pass (`packages/review/deep.py`): Nemotron Super reads the documents in parts of
12,000 characters with an instruction to "flag what a careful reviewer would raise", and every finding must quote the
document. It was **not** told to look for these clause types. Measured on 30 contracts sampled from the held-out half
(`benchmarks/cuad/results.json`, `ai` section), cost $0.46, with the prompt as it was at the time:

| Over 123 labelled clauses in those contracts | Recall |
|---|---|
| Fine Print rules | 56% |
| Nemotron Super, generic read | 24% |
| Either | **68%** |

The model found 9.6 items per contract, with precision 12.5% against the 15 mapped categories and 26.6% against any of
CUAD's 41. On this task the deterministic rules are the stronger detector and the model adds recall on clauses the rules
miss (non-compete, minimum commitment, IP assignment). 12 of the model's chunk calls failed even after retries, so those
contracts were read incompletely. The benchmark reads at most 8 parts (about 96,000 characters) per contract, and 5
contracts were longer than that. This weak result is why the clause pass in section 2 was built.

### 4. PARSEC on real earnings releases

```bash
python -m benchmarks.ebitda.fetch --companies 40    # 40 real 8-K releases with an Adjusted EBITDA reconciliation
python -m benchmarks.ebitda.run --split dev         # where the checks were tuned
python -m benchmarks.ebitda.run --split test        # held out, run once
python -m benchmarks.ebitda.fetch --companies 20 --start 2024-01-01 --end 2024-12-31 --set fresh_2024.json --exclude manifest.json --split test
                                                     # a fresh set of 20 companies not in the first set
python -m benchmarks.ebitda.run --split test --set fresh_2024.json
```

PARSEC's original extraction matched one exact layout (`Reported Unadjusted EBITDA:` and so on). On 40 real earnings
releases from SEC EDGAR it found a base EBITDA in **0**. When that layout is absent it now uses
`packages/workflow/bridge.py`: Nemotron Super reads the reconciliation table and returns the starting line, every
adjustment and the stated Adjusted EBITDA, and plain code then decides what to trust. Every line must be found in the
document with its value, and the lines must add up to the company's own stated total within rounding. A misreading
almost never adds up by accident, so no hand-made answer key is needed.

| Real earnings releases, one per company | Read completely and add up | Misread | Incomplete |
|---|---|---|---|
| Dev half (24, 2025), where the checks were tuned (`results_dev.json`) | 21 | 1 | 2 |
| First test half (16, 2025), run once with dev settings (`history/01_test_clean_before_running_text_fix.json`) | **13** | 0 | 3 |
| **Fresh held-out set (20 new companies, filed in 2024)**, run once (`history/results_fresh_2024_test_before_checker_fix.json`) | **16** | 0 | 3 (+1 unreadable reply) |
| Same three sets re-measured after the checker fixes below (`results_dev.json`, `results_test.json`, `results_fresh_2024_test.json`) | dev 21, test 15, fresh 17 | 0 | dev 2 (+1 unreadable), test 1, fresh 3 |

**After a code review (`benchmarks/ebitda/history/README_checker_fix.md`).** The checker was made stricter and fixed:
a row now verifies only if its label is on the same line as the value (a bare number could previously match any line),
"FY2024" style years are read, and values such as `20.0` or `(19.0)` were wrongly being dropped as years. The three
sets were then re-run. Because model replies vary between runs, every reply of the final run was also scored with the
old checker: only two releases changed because of the checker (Koppers and EnLink, both from incomplete to adds up,
the `20.0` fix); the other changes came from the model answering differently. That fix was found on these sets, so the
16 of 20 above remains the clean held-out figure.

Cost: about $0.005 per release. The old pattern-based extraction read none of the 60 test and dev releases.

How the test data was used, plainly: after the first test run I looked at its three failures. Two were releases that set
the reconciliation as running text rather than a table, and one label carried a footnote marker "(2)". Fixing those made
the checker handle those layouts (14 of 16 on the same half, `results_test.json`), but that half was then no longer
untouched, so the fixes were measured on a fresh set of 20 companies never seen before. Every result file is kept in
`benchmarks/ebitda/` (`results_*.json`, `history/`). The remaining failures are model errors the checker caught: a wrong
value for a row, or a line it could not place.

Tuning fixed the checker, not the model: models add `$` signs and write `0` for a dash, so a line is also accepted when
a document line with the same label holds exactly that value, or, in running text, when the label is followed directly
by that value as its first figure (a prior-period figure is refused).

**What PARSEC then checks, on the company's own rows (no model judgement):**

- *Add-backs to evidence*: every adjustment beyond interest, tax, depreciation and amortisation, with the value at stake
  at the chosen multiple when the units are stated.
- *Add-backs present in every period shown*: an adjustment called non-recurring that has a non-zero amount of the same
  sign in every period column of the table (for example stock-based compensation in all four columns of Franklin
  Covey's release). During development 37 of 50 real releases read had at least one such row, mostly stock-based
  compensation but also e.g. restructuring and impairment; that count was not saved to a result file. Columns can
  include year-to-date totals that contain the quarter, so this is a reason to ask, not proof that a cost recurs.
- *Add-backs that also appear in the ledger*: when an accounts or ledger document is supplied, each add-back is matched
  by wording against its lines, and one booked in two or more years is flagged with those lines as evidence. Real CIMs
  and ledgers are not public, so this check is covered by unit tests only, not by a real-data benchmark.

`GET /api/arena/parsec` serves `benchmarks/ebitda/results_test.json`, the first test half after the checker fixes
(15 of 16).

### 5. "What this file still needs" on eight real document sets

`python -m benchmarks.requirements.samples` runs the step (Nemotron Super, `document_requirements_json`) on a licence,
a franchise, an endorsement and a services agreement (CUAD), an SEC order, a 10-K, an earnings release and a small mixed
data room, and saves every list to `benchmarks/requirements/samples.json` for a practitioner to judge. The model sees
each document's name, its first 1,500 characters and up to 60 headings drawn from the whole document. There is no
answer key, so this is a record, not a score. The current `samples.json` is a 3-sample consensus run (`--samples 3`,
identical to `history/samples_consensus_run2.json`); the product default is a single call.

Reading the outputs found two real faults:

- For a 10-K the model called the financial statements, MD&A and auditor's report "missing", though they are sections
  of the 10-K, because it saw only the first 25 headings. It now sees headings from the whole document, and an item
  called missing whose name matches a heading in a supplied document is downgraded to "unclear" with that heading shown.
  The 10-K list in `samples.json` now asks for what sits outside a 10-K: the proxy statement, the latest 10-Q, recent
  8-Ks and debt covenant certificates.
- Long official headings such as "Item 7. Management's Discussion and Analysis..." were discarded as too long, so a
  10-K's own sections were reported missing. Fixed, with tests.

A code check also catches the model asking for a document that was supplied, by comparing each item with each supplied
document's name and title lines. Run over the 402 item names in `benchmarks/requirements/history/`, it fires on 4, each
time on an item that names the supplied agreement itself.

### 6. Findings memo (Nemotron Ultra)

"Draft memo" turns the verified findings and the request list into a short memo in one Ultra call
(`findings_memo_json`, about $0.02). Every point must cite the findings (F1, F2...) or open requests (N1...) it rests on,
and every number in a point must appear in the evidence it cites; points that fail either check are removed and counted
(`packages/review/memo.py`). Numbers written as words ("six months", "twelve") are checked the same way; "one" is not,
since it is usually a pronoun. On the two-document example, 5 of its 28 citations were spot-checked by hand and all 5
pointed at the right words; the rest were not checked, and this check was not saved to a file.

### 7. Working the way a deal team works

Research into how due diligence is organised (law-firm, accounting and data-room sources, cited in the code) shaped a
Workflow tab and the copied report (`apps/review/workflow.js`):

- **Request list** in data-room numbering (1.x corporate and legal, 2.x financial, ... 11 other), with priority,
  status (Outstanding / To confirm) and the reason for each item, exportable as CSV. Items come from the model's
  "what this file still needs" and from documents referred to but not supplied. The workstream of each item is assigned
  in code (from the sourced vocabulary in `config/dd_vocabulary.yaml` and fixed mappings), never by the model.
- **Q&A log** of draft questions to the seller, each with its citation, exportable as CSV (guarded against spreadsheet
  formula injection).
- **Suggested treatment** for each serious finding, from the categories in Paul Hastings' buyer's playbook (price
  adjustment, specific indemnity, condition precedent, escrow or holdback, post-completion covenant, further request).
  It is a keyword-based screening suggestion, labelled as such; the reviewer can replace it and the report shows both.
- **Sign-off**: a reviewer confirms each serious flag. The report stays DRAFT, and says why, until a reviewer is named,
  every critical and high flag that was not dismissed is confirmed, and no high-priority request is outstanding. The
  reviewer can mark a request Received or Not applicable with a required note; an audit trail lists every reviewer action.
- **Coverage by workstream**: documents supplied, findings and open requests per workstream, so an area with nothing
  to go on is visible.

These are checked by unit tests (`apps/review/workflow.test.js`); their usefulness to a deal team has not been measured.

### 8. Public-source check on real companies

`python -m benchmarks.review.web_live` queries Tavily and records the sources it cites in
`benchmarks/review/web_live.json`, with a retrieval timestamp. It refuses to run without a real key.

| Group | Result |
|---|---|
| Real companies with well-documented disputes (Wirecard, Boeing, Theranos, Volkswagen, Enron) | 5 of 5 found, each with public URLs |
| Invented company names | 0 of 3 found |

A match means public sources mention the company with a dispute term. It is not a legal finding. Results can concern a
different company with a similar name, and search results vary between calls.

### 9. Connecting documents (the Connections tab)

`apps/review/dossier.js` runs after the rules and links what the rules found across documents. It is not tied to one
domain; it works on any set of documents.

| Situation | What it does |
|---|---|
| Few documents | Says which cross-checks could not run, lists what each document refers to but does not include (schedules, side letters, exhibits), and lists the companion documents usually requested for that kind of document (labelled generic) |
| Normal set | Parties that appear in more than one document, the same amount in several documents, findings that touch the same party, a dated timeline |
| Many documents | Groups repeated problems, orders documents by how much attention they need, finds near-duplicates |

Known limits, found by running it with the rules on 66 real SEC orders (309,000 words, about 0.7 s in Node): party
detection relies on company suffixes (`Inc`, `Ltd`, `Holdings`...) so it misses individuals and trading names; figure
conflicts between documents that share no named party are marked, not removed. **Its accuracy on real due diligence
sets has not been measured.** The tests (`apps/review/dossier.test.js`) check behaviour on small hand-written strings.

### 10. Real fraud-case documents

```bash
python -m benchmarks.fraud.fetch --index              # all 3,300+ SEC accounting enforcement releases
python -m benchmarks.fraud.fetch --download 60 --since 2010
python -m benchmarks.fraud.filings --cases 20         # the companies' own 10-Ks for the years an order concerns
```

Downloads the SEC's own orders and complaints (public domain) into `data/fraud/` with a `manifest.json` of URLs and
sha256 hashes, and then the 10-Ks filed for the affected years. The orders describe fraud **after** it was found. The
10-Ks are what a reviewer would have had beforehand, but no scored test of whether the tool would have caught the
problem from them has been run or saved.

---

## Measured, and not shipped

Each of these was tried on real data and left off, or kept only as an option, because the measurement did not support it.

- **Ultra as a second opinion on clause tags.** `--confirm` has Nemotron Ultra re-check every Super tag. On the 25 dev
  contracts it rejected 42 of 203 tags, raising precision from 52% to 60%, but model recall fell from 78% to 74%
  (minimum commitment halved, 57% to 29%) and cost rose 76% (`benchmarks/cuad/history/05_confirm_dev.json`). For a tool
  whose job is to miss nothing that is the wrong trade, so it is not used in the product. The test half was not spent on it.
- **Reference vocabulary in the prompt.** Giving the model the sourced list of common requests made its lists worse in
  6 of 8 real document sets, by my reading: it replaced document-specific requests (an unredacted exhibit, a 10-Q and
  proxy for a listed company) with a generic buy-side checklist. The vocabulary is now used only to label workstreams in
  code. Before and after are in `benchmarks/requirements/history/` (`samples_with_vocabulary.json`,
  `samples_no_vocabulary_run1.json`).
- **Near-miss notes and "/no_think" for clause precision.** Together they raised dev precision from 50% to 60% at
  unchanged recall, but on 30 fresh held-out contracts precision was 45.6%, so the gain was not confirmed and both are
  off. A Nano yes/no gate cut recall in every variant. Results: `benchmarks/cuad/history/06_*.json`.
- **Nano triage and Ultra rulings in the generic AI read.** Tried during development, they showed no gain worth the
  extra cost (Ultra rulings cost several times as much as Super alone). That comparison was not saved as a result file.
  Both are off by default and can be switched on in the page.

### Second round: what was tried to fix the remaining limits

| Limit | What was tried | Result | Shipped? |
|---|---|---|---|
| Request lists change between runs | Ask 3 times, keep items at least 2 of 3 agree on (self-consistency) | Two 3-sample runs overlapped 0.26 (`samples_consensus_run1/2`) at 3x the cost; the single-call runs saved in `history/` overlap 0.15-0.16 with each other, though those pairs also differ slightly in prompt version (no clean pair of single-call runs was saved); at temperature 0.7 overlap was 0.33 only because lists shrank to 1-7 items; voting sometimes locked in the generic list | No (`samples=N` option kept) |
| Model asks for a document that was supplied | Code check against each supplied document's name and title lines | Over the 402 saved item names it fires 4 times, each on the supplied agreement itself | Yes |
| Clause precision (52%) | CUAD's own category definitions (from CUAD_v1.json) | +2.5 points on dev (53.7% to 56.2%), within run-to-run noise (the same plain prompt gave 50.2% and 53.7% on the same contracts), and lower recall in four categories | No |
| Clause precision | 3-pass voting at temperature 0.7 | -1.7 points on dev | No |
| Suggested treatments are keyword rules | The reviewer can replace each one; the report shows both | Works in the browser and tests | Yes |
| Requests could not be resolved | Reviewer marks a request Received or Not applicable, with a required note; an audit trail lists every reviewer action | Works in the browser and tests | Yes |

Overlap is measured by `python -m benchmarks.requirements.stability <run A> <run B>`. The main lesson: on 25 contracts,
run-to-run variation is about as large as the precision effects being tested, so smaller tweaks cannot be told apart from
noise without a larger sample. Results are in `benchmarks/cuad/history/07_*` and `benchmarks/requirements/history/`.

---

## Repository layout

```
apps/api/            FastAPI app: PARSEC routes, Fine Print AI/web/requirements/memo endpoints, results endpoints, static mounts
apps/review/         Fine Print (browser): engine.js rules, dossier.js connections, workflow.js deliverables, UI, tests, examples/
apps/web/            PARSEC dashboard
packages/
  common/            .env loader
  compute/           Decimal arithmetic: EBITDA bridge, concentration, footing
  evidence/          extraction, forensics (Benford, proof of cash), data-room tracker
  gateway/           Nebius client: routing, retries, fallback model, metering. Raises when unavailable
  review/            AI read, clause pass, requirements, memo, web check, access guard
  schemas/           Pydantic models
  tavily/            Tavily client for PARSEC (live only)
  workflow/          the PARSEC pipeline and the reconciliation reader (bridge.py)
config/              model routes and prices, thresholds, sector playbooks, due diligence vocabulary
benchmarks/
  cuad/              real-contract benchmark: loader, mapping, scoring, runners, results and history
  ebitda/            real 8-K earnings releases: fetch, runner, results and history
  fraud/             SEC enforcement releases and the companies' 10-Ks: fetchers and HTML-to-text
  requirements/      "what this file still needs" sample runs and the stability comparison
  review/            rules driver, live web evidence
scripts/check_env.py key and connectivity check
tests/               unit tests and integration tests on FinanceBench and FinQA samples
backtest/            FinanceBench and FinQA data used by the integration tests
```

`GET /api/arena/benchmark`, `/api/arena/web-evidence` and `/api/arena/parsec` serve `benchmarks/cuad/results.json`,
`benchmarks/review/web_live.json` and `benchmarks/ebitda/results_test.json`. Each returns `{"status": "not_run"}` with
the command to produce it if the file does not exist.

## Known limits

- **The generic AI read covers about the first 96,000 characters of a request.** It reads at most 8 parts of 12,000
  characters across all documents together, in order, so later documents in a large set may not be read by it; the page
  says when this happened. The clause pass (section 2) reads every document in full, and the browser rules read everything.
- The extra AI tiers (Nano triage, Ultra rulings) are off by default; see "Measured, and not shipped". `FEEDBACK.md`
  records what was observed with the models.
- Fine Print's rules are English-only regular expressions. They are screening, not legal advice.
- Prompt-injection detection (browser rules and PARSEC) is pattern matching: a paraphrased instruction is not flagged.
  The protection that holds is structural: quotes are verified against the documents and arithmetic is done in code.
- With no `REVIEW_API_TOKEN` set (the local default), the paid endpoints are open to anyone who can reach the server;
  the server warns about this at start-up when bound to a public address. PARSEC keeps up to 100 deals in memory. See
  `SECURITY.md`.
- The 10-Ks and earnings releases are converted from HTML by `html_to_text` in `benchmarks/fraud/filings.py`; tables
  become one line per row. The text of the SEC orders in `data/fraud/text/` (used for the Connections timing) was
  extracted from the downloaded PDFs by a step that is not in this repository.

## Files you may want to read next

- `ARCHITECTURE.md`: flowcharts of the system, one check, and the PARSEC reconciliation path.
- `SECURITY.md`: what is and is not protected.
- `FEEDBACK.md`: observations on Nebius, Nemotron and Tavily from real calls.
- `.env.example`: every configuration variable.
- `PARSEC_Plan_v3.md`: the original plan, written before any measurement. Its figures are projections, not results.

## License

Apache-2.0 (see `LICENSE`). CUAD is © The Atticus Project, CC BY 4.0, and is downloaded at run time, not redistributed here.
