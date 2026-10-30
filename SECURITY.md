# Security and trust: what this code does and does not do

This describes the behaviour of the code in this repository. It lists limits as plainly as protections.

## What it does

1. **Documents are untrusted input.**
   - In the generic AI read (`packages/review/deep.py`) each document part is wrapped in `<document ...>` tags, with any
     `</document` in the text neutralised, and the system prompt states that the contents are untrusted data and must
     not be followed. The other model calls (clause pass, "what this file still needs", memo, EBITDA reconciliation) do
     not use these tags; they rely on the output checks in point 2.
   - Text that tries to instruct a reviewer ("ignore previous instructions", fake `<system>` tags, "sign off all
     add-backs") is flagged as a finding by the browser rules (`apps/review/engine.js`) and by the PARSEC pipeline
     (`packages/workflow/pipeline.py`), and is never followed. This detection is pattern matching (see below).
2. **Model output is checked against the source before it is shown.**
   - AI read: a finding is kept only if the quote it cites is found in the document (exactly, or ignoring whitespace
     and dash/quote variants). Everything else is discarded and counted.
   - Clause pass: a hit is kept only if its quote is inside the passage it names.
   - EBITDA reconciliation: every line must be found in the document with its value, and the lines must add up to the
     stated total.
   - Memo: a point is removed unless it cites existing evidence and every number in it appears in that evidence.
   - "What this file still needs": an item is marked supplied only with a verified quote or document match.
3. **Arithmetic is not done by a model.** EBITDA bridges, concentration and footing are computed with Python `Decimal`
   (`packages/compute/lib.py`, `packages/workflow/bridge.py`); the browser rules do their sums in JavaScript.
4. **Evidence is traceable.** PARSEC findings drawn from documents carry the verbatim quote, its character offsets and
   the SHA-256 of the document it came from.
5. **Nothing is simulated.** With no key, or when a service fails, the result says it was unavailable. There is no
   mock model and no simulated web search.
6. **Keys stay on the server.** They are read from the environment or from `.env`, which is git-ignored; the loader
   (`packages/common/envfile.py`) never overrides variables already set and never prints values. The browser never
   receives the keys. `scripts/check_env.py` reports presence and length, never values. The Docker image copies only
   `apps`, `packages`, `config` and `benchmarks`, so `.env` is not baked in; keys are passed with `--env-file` at run time.
7. **Every endpoint that spends money, or reads or signs a stored deal, is guarded** (`packages/review/guard.py`,
   applied in `apps/api/main.py` to `/api/v1/diligence/*` when `ai` or `web` is set, `/api/v1/diligence/requirements`, `/memo`, `/deep`, the `/api/v1/data-room/*` and `/api/v1/companies/lookup` children, and their older paths `POST /api/review/deep`, `/api/review/requirements`, `/api/review/memo`,
   `POST /api/deals/run`, `GET /api/companies/lookup`, `GET /api/deals/{id}` and the sign endpoint):
   - an optional access code (`REVIEW_API_TOKEN`, sent as `X-Review-Token`, compared in constant time). If the variable
     is empty, no code is required, and the server logs a warning at start-up when it is also bound to a non-loopback
     address. Both pages ask for the code when the server requires one;
   - a per-client rate limit (`REVIEW_RATE_PER_MINUTE`, default 6 requests a minute, keyed on the client address); idle
     clients are pruned from the table;
   - a daily spend cap (`REVIEW_DAILY_BUDGET_USD`, default $2.00). Each paid request reserves an estimate ($0.05)
     before it runs and settles the actual cost afterwards, under a lock, so concurrent requests cannot all pass a
     nearly spent cap. PARSEC runs report their Nebius cost to the same cap.
   Request bodies are size-limited: up to 10 documents of 200,000 characters each (12 for the requirements step), names
   up to 200 characters, company names up to 200 characters. At most 100 PARSEC deals are kept; the oldest is dropped.
   Deal ids are random (`secrets.token_urlsafe`), not derived from the documents.
8. **Exports and display do not execute document text.** CSV exports prefix any cell starting with `=`, `+`, `-`, `@`,
   tab or carriage return with `'`, so a spreadsheet does not run it as a formula (`apps/review/workflow.js`). The
   Fine Print and PARSEC pages put document and model text into the page with `textContent`, not `innerHTML`.
9. **CORS** allows only `http://localhost:8000` and `http://127.0.0.1:8000` by default (`CORS_ORIGINS` to change),
   without credentials.
10. **Browser hardening.** Every response carries a Content-Security-Policy (scripts only from this server and
    cdnjs.cloudflare.com, no plugins, no framing), `X-Content-Type-Options: nosniff` and a strict `Referrer-Policy`.
    The two CDN libraries Fine Print loads on demand (pdf.js, mammoth) are pinned with Subresource Integrity hashes
    checked against cdnjs; the pdf.js worker is loaded by URL, which browsers cannot integrity-check. Links built
    from web-search results are shown only for `http(s)` URLs.

## Privacy

- The Fine Print rules run **in the browser**. Documents are not uploaded for them.
- The optional AI reads send document text to this server and from there to Nebius. The optional web check sends
  company names (not document text) to Tavily. The page says so when any of them is switched on.
- The page keeps the access code in `sessionStorage` and the reviewer's name in `localStorage` of the browser.
- The server keeps analysed PARSEC deals **in memory** for the life of the process. The API writes nothing to disk.

## What it does not do

- **No audit log.** An earlier version of this file described an append-only hash-chained audit log. The
  `AuditEvent` model exists but nothing writes to it. (The Fine Print report's "audit trail" lists the reviewer's actions
  in that browser session; it is not stored.)
- **With no access code set, the paid endpoints are open** to anyone who can reach the server. That is the default
  for local use; set `REVIEW_API_TOKEN` before exposing it, as the start-up warning says.
- **The guard is per process and in memory.** Several workers, or a restart, reset the rate limiter and the spend cap,
  and behind a proxy every client shares the proxy's address. The reservation bounds overspending but does not remove
  it: a request whose real cost exceeds its $0.05 estimate can take spending past the cap by that difference. Tavily
  calls are counted but not priced, and if a PARSEC run fails part-way its partial model cost is not recorded.
- **"Signed" in PARSEC is not an attestation.** Anyone with the access code (or anyone at all when none is set) can
  mark a finding signed.
- **No user accounts.** The access code is one shared secret, not per-user authentication.
- **Injection detection is pattern matching.** A paraphrased instruction will not be flagged. The defence that holds is
  structural: quotes are verified and arithmetic is done in code.
- **Public-source matches are name matches.** A company that shares a name with another can match the wrong entity.
- **No fine-tuning or retention is implemented**, but the third-party services receive what you send them. Review
  Nebius's and Tavily's terms before sending confidential material.

## Reporting

Open an issue in this repository with steps to reproduce. (`pyproject.toml` lists no contact address.)
