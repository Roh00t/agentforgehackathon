# ClauseGuard v2 — Known Issues

Captured during the Build Order + automated stress pass (2026-06-13).
**No P0 issues.** 35-test suite passes (functional 33/33; perf 3/3 under normal
API speed); the real 5-doc Xcellink case analyses end-to-end.

## ADDITIONS A–D — Restored privacy + sponsor guardrails (2026-06-13)

PII redaction (Daytona, **local-regex fallback**) and Terminal 3 attestation were
re-added to the analyze flow; MOM letter now uses fill-in placeholders; a second
synthetic fixture (`synthetic_unsigned_form.pdf`) enables a 2-file cross-document
demo. Re-verified end-to-end on synthetic files + **all 35 tests still pass** with
redaction in the pipeline. Sponsor integrations back to **4** (Bright Data, Daytona,
TokenRouter, Terminal 3).

### Additions P1 notes
- **Test-suite duration ~27 min** (was ~13 min). Redaction adds one Daytona
  sandbox round-trip per analyze-hitting test. This is *test* duration, not product
  latency — see next bullet. P1, infra-only.
- **Redaction product latency is small:** ~5s for 2 files run concurrently (Daytona).
  The ~117s seen on the synthetic 2-file analyze was the LLM call (TokenRouter latency
  variance), not redaction. Redaction adds ~5s on top of the existing analyze time.
- **Daytona dependency for the "in-sandbox" badge:** if Daytona is down, redaction
  silently falls back to identical in-process regex (privacy preserved, `engine:"local"`
  shown in the UI). Only the "ran in Daytona" demo point is lost on a fallback run.
- **Attestation is response-only** (not persisted): reloading an old session from the
  sidebar won't show the receipt. Fresh analyses show it. Acceptable for the demo.
- **Name redaction limitation unchanged:** regex catches NRIC/email/phone/address, NOT
  names in prose, signatures, or company names — surfaced in the UI redaction banner.

## AMENDMENT — Dual-Panel + Combined Dispute Judgment (2026-06-13)

The single `/api/analyze` (`files`) endpoint became a dual-panel one
(`contract_files` required + `context_files` optional PDF/TXT/EML), returning
ONE combined `{analysis, judgment}` from a single LLM call. The judgment renders
above the red flags; sessions store/reload it; the sidebar shows a verdict label.

**Real 5-file Xcellink demo:** EMPLOYER_AT_FAULT, HIGH confidence, 6 CRITICAL
red flags, ~96s. Judgment cites the unsigned training form, the Jan→May training
delay, the uncountersigned LOA, the 28 May intimidation meeting, and Albert Lim's
email admission.

### Amendment P1 notes
- **Combined-call robustness.** Haiku occasionally emits malformed JSON; when
  valid it is correct. `analyze_combined()` retries up to 3x on a parse/validation
  failure (timeouts are NOT retried). This made the functional suite reliable.
- **max_tokens=16000.** The 5-doc combined output is ~8k tokens; at the old 8192
  cap it truncated → invalid JSON. Raised to 16000. A *much* larger upload could
  still truncate (would surface as a 502 after retries, never a crash).
- **Perf tests are latency-sensitive to TokenRouter.** On a normal run all 35
  pass; during one overloaded window (23.7min vs 12.8min) the two wall-clock perf
  tests flaked (a single Haiku call exceeded the 180s budget). Functional
  correctness is unaffected. Budget is tunable via `CLAUSEGUARD_TEST_BUDGET`.
- **Default model = Haiku 4.5** (was Sonnet) for demo speed — env-overridable.

### Amendment — deviations from the brief's literal code (intentional)
- **No double-wrapping.** `main.py` passes RAW text; `analyze_combined` wraps once
  and dedups on raw text (the brief wrapped in both places, which also broke the
  cross-panel md5 dedup since the wrapper embeds the filename).
- **Timeout 90s → 180s.** The brief's 90s would 504 the real multi-doc case.
- **max_tokens 6000 → 16000.** 6000 would truncate the combined output.

---


## Stress-test results (Part C)

| Test | Status | Notes |
|------|--------|-------|
| Health endpoint | PASS | |
| Regulations endpoint (>=4 regs) | PASS | 8 regs, cached |
| Non-PDF rejection (400) | PASS | |
| Fake PDF magic bytes rejection (400) | PASS | |
| Oversized file rejection (413) | PASS | |
| Too many files rejection (400) | PASS | >10 files |
| Blank PDF -> 422 (not 500) | PASS | |
| Single contract analysis | PASS | ~47s on Sonnet |
| Multi-file analysis | PASS | fixed a test-fixture em-dash bug (see below) |
| Session save after analysis | PASS | |
| Session retrieval by ID | PASS | |
| Unknown session -> 404 | PASS | |
| Prompt injection -> flags still found | PASS | injection ignored, flags produced |
| Huge PDF -> truncated not crashed | PASS | text capped at 8000 chars/doc |
| Path traversal filename -> safe | PASS | filename is display-only |
| SQL injection via session ID -> safe | PASS | parameterised queries |
| Regulations endpoint < 500ms | PASS | served from cache |
| Analysis completes < budget | PASS | single ~47s, 5-doc ~108s |
| 3 sequential analyses all succeed | PASS | |

## P1 issues (logged, not blocking)

- **Analysis latency.** Sonnet via TokenRouter: ~47s/single doc, ~108s/5 docs.
  Good for a demo, not instant. Set `CLAUSEGUARD_MODEL=anthropic/claude-haiku-4.5`
  to roughly halve it at a small quality cost. (Kimi K2.6 was ~190s — too slow —
  so the default model was switched to Sonnet, still via the TokenRouter key.)
- **Session auth.** Anyone who guesses an 8-char hex session ID can view that
  analysis. Acceptable for a hackathon; add real auth for production.
- **CORS = `*`.** Fine for a localhost demo; restrict the origin in production.
- **Rate limiting is per-IP.** Behind a NAT all users share one bucket. The
  automated suite raises the limit via `CLAUSEGUARD_RATE_LIMIT` (conftest) so it
  isn't throttled; production default is 5/min on `/api/analyze`.
- **Scraper fallback.** mom.gov.sg may 403 the `requests` fallback; the hardcoded
  KB (5 entries) guarantees the app never shows 0 regulations. 3 of 6 URLs
  scraped live on this run; the rest came from KB.
- **Scanned/image-only PDFs** return no text -> a clean 422, not a crash. Users
  need text-layer PDFs.
- **Deprecation warnings** (`on_event`, TestClient `httpx`) are cosmetic only.

## Deviations from the brief worth knowing

- **Model:** brief said Kimi-fallback / Anthropic-primary. Anthropic isn't keyed,
  and Kimi was too slow, so the analyzer routes to `anthropic/claude-sonnet-4.6`
  **through the existing TokenRouter key** — Claude quality/speed, no new key.
- **Test fixture fix:** the verbatim `make_pdf` helper crashed on a `—` (em-dash)
  because Helvetica is latin-1 only. `make_pdf` now maps Unicode punctuation to
  ASCII before rendering. No change to app behaviour.
- **`response_format=json_object`** is NOT used — TokenRouter/Kimi returned empty
  content with it. Robust fence-stripping + 8192 max_tokens is used instead.

## TASK 2 — NER cross-document entity map (2026-06-14)

Added `backend/entity_map.py` (regex + spaCy `en_core_web_sm` PERSON/ORG), built
once over ALL session text combined so each entity gets ONE consistent placeholder
across files. Applied before the existing Daytona/local regex sweep; the entity map
is returned to the browser for client-side MOM-letter de-redaction and is **NOT
persisted** (guardrail #3). `.eml` now extracts the parsed body (stdlib `email`),
not raw MIME. Verified: NRIC/email/phone/address/person-name redacted before the LLM,
cross-document consistency holds, browser de-redaction leaves zero placeholder tokens.

### P1 — `en_core_web_sm` is a small, imperfect NER model
- **Under-redaction (names/orgs in prose):** company names like "Acme Staffing" are
  NOT caught — spaCy misses them. This is the documented best-effort limit (disclosed
  in the UI banner). A heavier NER (`presidio` or a larger spaCy model) or a
  `Pte Ltd|Ltd|Inc` org-suffix regex would close most of it — deferred (scope/ask).
- **Over-redaction / noisy spans:** spaCy mis-tagged generic words/headings
  ("Company", "Employee Particulars", colon/newline-crossing spans). Mitigated by a
  noise filter in `entity_map.py` (`_is_noise_entity`: drops <2/>40-char spans, spans
  with newlines/colons, and a small stop-word set). Residual mislabels remain
  (e.g. job title "L1 Support" tagged ORG) — harmless: de-redaction restores them and
  privacy is unaffected (over-redaction fails safe).
- **No session-reload de-redaction:** entity map isn't persisted (guardrail #3), so
  reloading an old session shows placeholders, not real names — same class as the
  already-accepted "attestation not persisted" P1.
- **Latency:** spaCy NER on combined text adds ~1s; analyze stays ~50–60s/doc (LLM-bound).
- **No chat textbar exists** in the app, so Task 2.2's "chat input redaction" sub-item
  had no target and was intentionally skipped (not built — would be new scope).
