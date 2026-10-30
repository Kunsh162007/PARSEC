# Feedback for Nebius Token Factory, NVIDIA Nemotron and Tavily

Written from what was observed while building and benchmarking this project against real contracts and live
APIs. Every item below was seen in a real call; none is a guess. Some counts come from saved benchmark results in
`benchmarks/` (the file is named where so); others come from development runs whose output was not saved, and are
marked "(not saved)".

## Nebius Token Factory and the Nemotron 3 family

All three tiers (Nano 30B, Super 120B, Ultra 550B) answered live on one API key through the OpenAI-compatible
`/chat/completions` endpoint, and `usage` was always returned. That made per-tier token and cost metering simple.

### 1. Ultra sometimes returns its reasoning as the answer
- **Seen:** with a "return only JSON" instruction and `max_tokens: 500`, roughly half of Ultra calls put chain-of-thought
  prose in `message.content` and ran out of tokens before the JSON. 7 of 13 replies could not be parsed (not saved).
- **Workaround that helped:** prefixing the system prompt with `/no_think` and adding "output the JSON object and nothing
  else". That reached 11 of 12 parseable (not saved).
- **Ask:** a documented request parameter to switch reasoning off, and reasoning returned in a separate field so
  `content` is always the final answer. A prompt convention is easy to miss and not discoverable from the API reference.

### 2. Malformed JSON at temperature 0, and non-determinism
- **Seen:** Super intermittently emitted JSON with a string value missing its opening quote
  (`"why": An unlimited clause ...`), making the whole reply invalid. The same request at temperature 0 returned valid
  JSON on some calls and invalid on others (4 of 8 calls valid before a tolerant parser was added; not saved).
- **Ask:** a structured-output / JSON-schema mode that guarantees syntactically valid output, and a note on what
  determinism to expect at temperature 0.

### 3. Quotes are not verbatim
- **Seen:** when asked to copy text exactly, models replaced hyphens with non-breaking hyphens (U+2011) and straight
  quotes with curly quotes. Exact-match verification of the quote then failed on otherwise correct findings.
- **Ask:** guidance (or a mode) for faithful extraction, since evidence-linked outputs are a common use.

### 4. Throttling under parallel calls
- **Seen:** reviewing a long contract in 8 parallel chunk calls, with several contracts in flight, produced failures
  that looked like throttling (12 of 30 contracts lost, `benchmarks/cuad/history/03_tuned_test_with_model.json`).
  Retrying on 429/5xx with backoff reduced it: the re-run scored all 30 contracts, but 12 chunk calls still failed
  (`benchmarks/cuad/results.json`, `ai.usage`). The later clause pass, with smaller calls, had 0 failed calls out of
  154 (`benchmarks/cuad/history/04_verify_test.json`).
- **Ask:** documented rate limits per key and tier, and `Retry-After` on 429 so clients can back off precisely.

### 5. What worked well
- A configured fallback model per task (Ultra to Super) degraded cleanly when the primary was unavailable.
- Per-call `usage` made cost easy to measure per tier. On a short review, Super alone cost a fraction of a cent and
  adding Ultra rulings cost about 3.6 times as much (not saved), which made the "does the expensive tier earn its cost"
  question easy to answer. Measured on clause tags, an Ultra second opinion added 76% to the cost for lower recall
  (`benchmarks/cuad/history/05_confirm_dev.json`).

## Tavily Search

- **Seen:** the same query for the same company returned different result sets on different calls. A search for
  "Boeing Company" returned results with dispute terms in 8 of 8 results on one call and none in the snippets of five
  results on another (not saved). Fetching 8 results instead of 5 and retrying once made it stable across repeated
  runs; the saved run found 5 of 5 known companies (`benchmarks/review/web_live.json`).
- **Seen:** the results were genuinely useful as evidence: court dockets, regulator releases and the company's own
  annual report, with URLs and snippets.
- **Seen:** a search for a fictional "Titan Cloud Inc" returned a real, unrelated company ("Titan Cloud Software, LLC").
  Name matching cannot disambiguate entities.
- **Ask:** an entity-resolution aid (a domain or registry identifier as a filter), and a recency or "as of" field on
  each result so a stale item is not read as current.

## Methodology note

The benchmarks that produced most of these observations are in `benchmarks/cuad/` (510 real contracts,
lawyer-labeled clauses), `benchmarks/ebitda/` and `benchmarks/review/`. The first version of this file made latency
claims that had never been measured, and they were removed.
