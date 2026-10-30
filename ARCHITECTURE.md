# Architecture

Fine Print is one FastAPI server and two browser pages (*My deals* and the review page). Every API endpoint is a child
of `GET /api/v1`. The diagrams of the whole system and its workflows are in the [README](README.md#architecture); this
file is the detail behind them.

The core rule: **the model proposes; code decides what is kept.** Every Nemotron answer is checked against the source
by plain code, and anything that fails is dropped and counted.

## Where each part runs

| Part | Where | Files |
|---|---|---|
| Rules in ten areas, connections across documents, request list, Q&A log | Browser; the API runs the same `engine.js` through Node | `apps/review/engine.js`, `dossier.js`, `workflow.js`, `packages/review/rules.py`, `rules_runner.js` |
| PDF pages with marks, scanned-page OCR | Browser (pdf.js, tesseract.js served by this server) | `apps/review/pdfview.js`, `app.js` (`readPdf`, `ocrPage`) |
| Deal folders: list, read, save the review, export marked copies | Server, confined to `DEALS_DIR` | `packages/deals/workspace.py`, `export.py`, `apps/api/deals_routes.py` |
| Parsec: phrase matcher, wake word, screen commands | Browser | `apps/review/assistant.js`, `apps/home/voice.js`, `apps/review/deals.js` |
| Nemotron calls | Server, through one gateway | `packages/gateway/llm.py`, routes in `config/models.yaml` |
| Guard: access code, rate limit, daily budget, host check | Server | `packages/review/guard.py`, `apps/api/main.py` |

## Nemotron routes

| Task | Route | Model, fallback | Status |
|---|---|---|---|
| What the file still needs | `document_requirements_json` | Super 120B, Nano 30B | on |
| Clause pass over whole contracts | `clause_verification_json` | Super, Nano | on |
| Generic AI read | `document_review_json` | Super, Nano | on |
| Adjusted EBITDA reconciliation | `ebitda_bridge_json` | Super, Nano | on |
| Findings memo | `findings_memo_json` | Ultra 550B, Super | on |
| Committee questions | `ic_simulator` | Ultra, Super | on |
| Parsec's free questions and new wording | `assistant_command_json` | Super, Nano | on |
| Nano triage of long documents, Ultra rulings on findings, Ultra second opinion on clauses | `document_triage_json`, `document_adjudication_json`, `clause_confirmation_json` | Nano, Ultra | measured and off |

All routes call Nebius Token Factory's OpenAI-compatible `/v1/chat/completions`. The gateway retries throttling and
server errors, treats an empty answer (a reasoning model out of tokens) as a failure, asks the fallback model, reads
`usage` from every reply and prices it from `pricing_per_million_tokens`.

## What code checks

| Model output | Check | On failure |
|---|---|---|
| A finding's quote | Must be found in the document part it came from (quotes normalised for curly quotes and hyphens) | Dropped and counted |
| A clause tag | Its quote must be inside the numbered passage it names | Discarded |
| A reconciliation line | Label and value on one document line; lines must add up to the stated total in `Decimal` | Marked incomplete, or "check by hand" |
| A memo point | Must cite real findings or requests; every number must appear in the cited evidence | Point removed |
| A missing-document item | Compared with supplied names and headings | Downgraded to "unclear" |
| A Parsec command | Action from a fixed list; folder and document names that exist; an answer must cite real findings; remove, confirm and export are never done on the model's say-so | Refused, or the user is asked to say it plainly |

## A deal folder on disk

```
~/Deals/
  Orion Analytics/
    01 Financial/Confidential Information Memorandum.pdf
    ...
    .fineprint/review.json        saved review: findings, removed marks, decisions, document fingerprints
    _marked/summary.html          export: every problem, linked to the marked copies
    _marked/<document>.marked.html
```

Folders and files starting with `.` or `_` are not documents. A saved review is reused only while every document's
fingerprint matches; when documents change they are checked again, and marks removed before stay removed (matched by
what they say and where).

## Requests that cost money

Every paid route goes through the guard: the access code when `REVIEW_API_TOKEN` is set, a per-client rate limit, and a
budget reservation charged with the real cost of the call. Measured results, and the experiments that did not hold up,
are in [`docs/MEASURED_RESULTS.md`](docs/MEASURED_RESULTS.md); protections and limits in [`SECURITY.md`](SECURITY.md).
