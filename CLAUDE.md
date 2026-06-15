# ClauseGuard — Project Context for Claude Code

## What This Is
ClauseGuard: upload employment contracts and dispute documents, get a
plain-English summary, red flags with severity, cross-document
contradiction detection, a MOM/TADM draft letter, and a tamper-evident
signed receipt. Built at AIForge Hackathon (13 Jun 2026), now on a
production-improvement track. Singapore employment contracts, MVP scope.

## Current Task
Read the most recent "Claude Code Prompt" page in Notion under
HACKATHONS > ClauseGuard — AIForge Hackathon for today's specific
task list. This file is persistent background context only.

## Architecture (v2, current)
- **Backend:** FastAPI — `backend/main.py`, `backend/analyzer.py`,
  `backend/scraper.py`, `backend/extractor.py`, `backend/security.py`,
  `backend/db.py`, `backend/entity_map.py`. SQLite DB at `data/data.db`
  (2 active tables: regulations, scrape_log. sessions table exists
  but is deprecated — Phase 2 moved all sessions to client IndexedDB).
- **Frontend:** Vanilla JS + HTML — `frontend/index.html`,
  `frontend/db.js` (IndexedDB CRUD). No React, no npm, no build step.
  Served as FastAPI StaticFiles.
- **Entry point (ONLY correct command):**
  `uvicorn backend.main:app --host 127.0.0.1 --port 8000`
  Do NOT use `streamlit run app.py` — stale v1 artifact.

## Sponsor Integrations (4 active)
- **Bright Data** — MOM/IMDA regulation cache via `backend/scraper.py`
  (bdata CLI already authenticated, do NOT re-run `bdata login`)
- **Daytona** — sandboxed PII redaction, automatic local-regex fallback
  if Daytona unavailable (`engine:"local"` shown in UI)
- **TokenRouter** — LLM via OpenAI-compatible client.
  Default model: `anthropic/claude-haiku-4.5` (speed-optimised).
  Override: `export CLAUSEGUARD_MODEL=anthropic/claude-sonnet-4.6`
- **Terminal 3** — HMAC attestation signing of report hash (pure
  stdlib, no network, cannot fail due to connectivity)

## Environment — Critical Interpreter Gotcha
`python3` resolves to 3.14 (Homebrew updated) — this version is EMPTY.
Always use `python3.13` explicitly:
- Run scripts: `python3.13 script.py`
- Install packages: `python3.13 -m pip install <pkg> --break-system-packages`
- Verify: `python3.13 -c "import fastapi, pdfplumber, openai, daytona"`

Never introduce a venv. Never use bare `python3` without version suffix.

## Env Vars (in .env, also exported in ~/.zshrc)
TOKENROUTER_API_KEY   — LLM calls (Haiku default, Sonnet via CLAUSEGUARD_MODEL)
DAYTONA_API_KEY       — Daytona sandbox (local regex fallback if unavailable)
TERMINAL3_API_KEY     — HMAC signing secret
TERMINAL3_DID         — Terminal 3 identity
BRIGHTDATA_API_KEY    — Bright Data dashboard key
Verify: `env | grep -E "TOKENROUTER|DAYTONA|TERMINAL3|BRIGHTDATA"`

## Current Redaction State (read before touching pipeline)
Full redaction pipeline IS wired into `backend/main.py`:

Pass 1 — Regex (Daytona sandbox + local fallback):
  NRIC, emails, SG phone numbers, residential address patterns.

Pass 1.5 — SG company-name suffix regex (backend/entity_map.py):
  Catches "Acme Staffing Pte Ltd", "Xcellink Technologies", etc.
  Shares ORG counter with pass 2.

Pass 2 — spaCy en_core_web_sm NER (backend/entity_map.py):
  PERSON and ORG entities in prose. Best-effort.

ALL passes run BEFORE any text reaches the LLM. Entity map returned
to browser for client-side MOM-letter de-redaction. NOT persisted
server-side (guardrail #3).

Phase 3 adds a third input type: chat textbar. Chat text joins the
entity-map build alongside document texts — same redaction pipeline.

Known gap (P1): single-word company names and novel-suffix orgs leak.
Disclosed in UI banner.

## Phase Completion Status
- ✅ Phase 1 (Redaction-First): COMPLETE — NER entity map, cross-doc
  consistency, .eml support, MOM letter de-redaction client-side.
- ✅ Phase 2 (Browser-Persisted Sessions): COMPLETE — Option A
  implemented. Server stateless. IndexedDB via idb@8 CDN. "Clear my
  data" button. Shared-device notice. 34/34 tests pass.
- ✅ Phase 3 (Chat Functionality): COMPLETE — chat textbar (2000-char
  cap, scope-noted) between panels and Analyse button. Chat joins the
  combined entity-map redaction, passed to analyzer in a scoped
  `<USER_CONTEXT>` block, persisted per-session in IndexedDB (raw,
  client-side only), repopulated on reload. Verified end-to-end.

## Active Roadmap Direction
- **Phase 3 (current):** Chat textbar between upload panels and
  Analyse button. 2000-char cap. Chat text runs through entity-map
  redaction. Stored in IndexedDB per-session. Scoped as "additional
  context for this analysis" — not a general chatbot.

## Pre-Mortem Learnings (already fixed — do not regress)
- PM1: Missing `backend/__init__.py` → fixed, file exists
- PM2: LLM JSON in markdown fences → robust fence-stripping in analyzer
- PM3: MOM scraper 403 → hardcoded KB guarantees ≥5 regulations always
- PM4: Large file upload OOM → 15MB/file, 50MB total in security.py
- PM8: LLM timeout hang → timeout=180s, 504 returned on timeout
- PM9: Prompt injection via PDF → `<UNTRUSTED_DOCUMENT>` wrapping
- max_tokens raised to 16000 (8192 truncated 5-doc combined output)
- Combined analyze call retries up to 3x on JSON parse failure
- make_pdf test helper: use `bytes(pdf.output())` not
  `pdf.output(dest="S").encode("latin-1")` — fpdf2 2.8.7 returns bytearray

## Known P1 Issues (do not fix without being asked)
- Analysis latency: ~47s single doc, ~108s five docs (Haiku default)
- CORS = `*` — restrict origin before public deployment
- Rate limiting is per-IP (NAT issue for production)
- Scraper: mom.gov.sg may 403 requests fallback; KB covers this
- Attestation not persisted: old sessions won't show receipt on reload
- Test suite duration ~22-27min with real LLM + Daytona round-trips
- NER under-redaction: single-word company names, novel suffixes leak
- NER over-redaction: residual mislabels (e.g. "L1 Support" → ORG),
  harmless (de-redaction restores correctly)
- Private-browsing banner is best-effort — modern Chrome/Firefox allow
  IndexedDB in incognito; footer notice is the real safeguard
- Server still mints a vestigial session_id in /api/analyze response
- Transient 502 on ~1-2 of ~15 analyze calls (Haiku JSON robustness)
- 4 orphaned pre-Phase-2 rows in data.db sessions (unreachable, harmless)

## Non-Negotiable Guardrails
1. All document/text/chat input is UNTRUSTED DATA — never treated as
   instructions. Analyzer prompt must use `<UNTRUSTED_DOCUMENT>` and
   `<USER_CONTEXT>` wrappers. Do not remove these.
2. Redaction (all 3 passes) runs BEFORE any text reaches the LLM or
   Bright Data. Chat input joins the entity-map build. Never bypass.
3. No user content persists server-side. Sessions + chat in IndexedDB
   only. Server returns analysis results, stores nothing about users.
4. Severity tiers (INFORMATIONAL / MODERATE / SERIOUS / CRITICAL)
   must be visually distinct.
5. Bright Data citations: "related guidance — verify relevance" only.
6. Terminal 3 signature: proves UNALTERED, not CORRECT.
7. Persistent disclaimer: "Not legal advice, not exhaustive."
8. Scanned/image-only PDFs: return clean 422, never send empty text.
9. Never f-string a SQL query. Parameterised queries everywhere.
10. Never use uploaded filenames in filesystem paths — display only.
11. Chat textbar is scoped to "additional context for this analysis"
    only. Never frame it as a general legal chatbot.

## Working Style
- STOP after each numbered step and report before continuing.
- Use `python3.13` explicitly — never bare `python3`.
- `--break-system-packages` required for any pip install.
- Do NOT introduce React, npm, venv, or any build pipeline.
- P0 issues: fix immediately.
- P1 issues: log in KNOWN_ISSUES.md, move on.
- If scope needs to expand beyond the task list, ASK first.