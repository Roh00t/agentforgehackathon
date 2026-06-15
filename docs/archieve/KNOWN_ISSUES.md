## TASK 2 PATCH — SG Company-Name Regex (2026-06-14)

Added `_SG_COMPANY_RE` to `backend/entity_map.py` as pass 1.5 (between the existing
regex pass and the spaCy NER pass). Catches company names with SG-common suffixes:
`Pte Ltd`, `Sdn Bhd`, `Ltd`, `Inc`, `Corp`, `LLP`, `Group`, `Holdings`, `Ventures`,
`Services`, `Solutions`, `Staffing`, `Consulting`, `Technology/Technologies`,
`Systems`, `Management`, `Capital`, `Partners`, `Associates`, `Enterprise/Enterprises`.

Uses `"ORG"` as the entity type to share the counter with the spaCy ORG pass —
`[ORG_N]` numbering is globally consistent across both passes. The `assign()` guard
(`if value in emap: return`) ensures no double-counting if spaCy also catches the
same name.

**Remaining gap (P1):** Single-word company names ("Xcellink") and names with
unlisted suffixes still not caught. Disclosed in UI banner. Closing this fully
requires presidio or a larger spaCy model — deferred.

## PHASE 2 — Browser-Persisted No-Auth Sessions (2026-06-14, in progress)

Moving all user-session storage from server-side `data/data.db` to client-side
IndexedDB (`idb` library via CDN). Server becomes stateless with respect to user
content. Privacy claim upgrades from "we protect your data" to "we don't have
your data."

### Architecture
- `frontend/db.js` — IndexedDB schema and CRUD (saveSession, getAllSessions,
  getSession, deleteSession, clearAllSessions)
- `backend/main.py` — sessions table INSERT removed; regulations and scrape_log
  writes unchanged
- `GET /api/sessions/:id` — returns 410 Gone (deprecated, not deleted)
- "Clear my data" button added to UI
- Shared-device / private-browsing notice added on page load

### P1 notes (to be updated on completion)
- QuotaExceededError handling: catch and surface to user
- Private/incognito mode: IndexedDB unavailable — sessions not persisted, notice shown
- Old server-side session IDs in any cached sidebar state → 410 Gone on click

## TASK 2 PATCH — SG Company-Name Regex (2026-06-14)

Added `_SG_COMPANY_RE` to `backend/entity_map.py` as pass 1.5 (between the existing
regex pass and the spaCy NER pass). Catches company names with SG-common suffixes:
`Pte Ltd`, `Sdn Bhd`, `Ltd`, `Inc`, `Corp`, `LLP`, `Group`, `Holdings`, `Ventures`,
`Services`, `Solutions`, `Staffing`, `Consulting`, `Technology/Technologies`,
`Systems`, `Management`, `Capital`, `Partners`, `Associates`, `Enterprise/Enterprises`.

Uses `"ORG"` as the entity type to share the counter with the spaCy ORG pass —
`[ORG_N]` numbering is globally consistent across both passes. The `assign()` guard
(`if value in emap: return`) ensures no double-counting if spaCy also catches the
same name.

**Remaining gap (P1):** Single-word company names ("Xcellink") and names with
unlisted suffixes still not caught. Disclosed in UI banner. Closing this fully
requires presidio or a larger spaCy model — deferred.

## PHASE 2 — Browser-Persisted No-Auth Sessions (2026-06-14, in progress)

Moving all user-session storage from server-side `data/data.db` to client-side
IndexedDB (`idb` library via CDN). Server becomes stateless with respect to user
content. Privacy claim upgrades from "we protect your data" to "we don't have
your data."

### Architecture
- `frontend/db.js` — IndexedDB schema and CRUD (saveSession, getAllSessions,
  getSession, deleteSession, clearAllSessions)
- `backend/main.py` — sessions table INSERT removed; regulations and scrape_log
  writes unchanged
- `GET /api/sessions/:id` — returns 410 Gone (deprecated, not deleted)
- "Clear my data" button added to UI
- Shared-device / private-browsing notice added on page load

### P1 notes (to be updated on completion)
- QuotaExceededError handling: catch and surface to user
- Private/incognito mode: IndexedDB unavailable — sessions not persisted, notice shown
- Old server-side session IDs in any cached sidebar state → 410 Gone on click
## PHASE 2 — COMPLETE (2026-06-14) — supersedes the in-progress notes above

Implemented **Option A**: server-side session writes removed entirely; `data.db` keeps
only `regulations` + `scrape_log`. All session CRUD is client-side IndexedDB
(`frontend/db.js`, `idb@8` via CDN). `/api/analyze` sets `X-Session-Storage: client`;
`GET /api/sessions` and `GET /api/session/{id}` return **410 Gone** (kept, not deleted).
"Clear my data" button + persistent shared-device footer notice + best-effort
private-browsing banner added.

Verified end-to-end in a real browser (Chrome MCP): real PDF upload -> `/api/analyze` ->
saved to IndexedDB -> sidebar from IDB -> **persists across reload** -> "Clear my data"
empties it. `data.db` sessions count **unchanged (4->4)** after a browser analysis.
**Bonus:** MOM-letter de-redaction now works on session reload (entity map lives
client-side), fixing the prior "no session-reload de-redaction" P1.

### P1 notes (Phase 2)
- **Private-browsing banner is best-effort.** Modern Chrome/Firefox ALLOW IndexedDB in
  incognito, so the probe succeeds and the banner won't fire there. It only triggers when
  IndexedDB genuinely throws (storage blocked / some browsers). The persistent footer
  notice ("stored in this browser only...") is the real shared-device safeguard, always shown.
- **Module-script timing bug (fixed during build).** The deferred module `<script>` set
  `window._idbLib` AFTER the classic `init()` ran, so the probe spuriously showed the banner
  on every normal load. Fixed: `checkStorageAvailability()` imports the idb module directly.
- **QuotaExceededError handled:** save catch shows a "storage full -> Clear my data" toast;
  other save errors show a generic toast (NOT re-thrown, so a save failure can't masquerade
  as the handler's "Network error").
- **4 orphaned pre-Phase-2 rows remain in `data.db` sessions** (from before this change).
  Unreachable now (read endpoints are 410) and harmless, but slightly undercut the "we don't
  have your data" claim. Can be cleared on request (data deletion -> left for user to confirm).
- **Server still mints a `session_id`** in the `/api/analyze` response; vestigial (client
  generates its own `crypto.randomUUID()`).
- **Cross-origin IndexedDB** is per-origin: production must serve from one stable origin.

### Deviations from the literal brief
- Stored raw `data.entity_map` ({real->placeholder}), NOT the brief's `invertEntityMap(...)`
  (which doesn't exist) — `renderAnalysis` inverts it itself; pre-inverting would double-invert.
- Adapted to real function names (`loadSessions`/`loadSession`/`renderAnalysis`/`showToast`)
  vs the brief's illustrative `renderSidebar`/`renderResults`/`showNotice`.

## PHASE 2 STRESS TEST — Part A results (2026-06-14)

Ran `CLAUSEGUARD_TEST_BUDGET=180 python3.13 -m pytest tests/test_backend.py` (34 tests, 22:21).
Result: **32 passed, 2 failed**; the 2 failures **passed on isolated re-run** -> transient, not bugs.
Effective result: **34/34 green**. No P0s.

All Phase-2-specific tests PASS: no server session write, `X-Session-Storage: client` header,
`/api/session/{id}` -> 410, regulations table still written, `entity_map` present in response,
sessions list 410-or-empty. Security tests pass: prompt-injection still flags, NRIC not in
response (redaction), SQLi/XSS/path-traversal safe.

### P1 notes
- **Test helper was stale (fixed).** `make_pdf` used `pdf.output(dest="S").encode("latin-1")`,
  but fpdf2 2.8.7 returns a `bytearray` (no `.encode`) -> every PDF-building test errored in
  setup (23 false failures, whole suite in 0.8s). Fixed to `bytes(pdf.output())` (test-only;
  the STRESS_TEST.md embedded helper has the same stale line and should be updated there too).
- **Transient 502 on ~1-2 of ~15 analyze calls.** `analyze_combined` 502s when the LLM returns
  malformed JSON even after its 3 retries (known Haiku-JSON P1). Surfaces as flaky failures in
  `test_contract_only_returns_insufficient_judgment` and the X-Session-Storage test (the latter
  502'd before reaching its header assertion; header itself verified working via curl + re-run).
  Not a regression. Mitigation already in place (retry x3); a 4th retry or stricter JSON-mode
  would reduce it further.
- **Suite duration ~22 min** (real LLM + Daytona per analyze test). Expected; budget-tunable.
## PHASE 2 STRESS TEST — Part B Browser Tests (2026-06-14, pending)

Part A: 34/34 automated backend tests pass (2 transient 502 flakes re-ran clean).
Part B: browser UI tests (STRESS_TEST.md Tests 1-13) pending — to be run next session.

### make_pdf test helper fix (2026-06-14)

`make_pdf` in STRESS_TEST.md and `tests/test_backend.py` used:
```python
return pdf.output(dest="S").encode("latin-1")
```
fpdf2 2.8.7 returns a `bytearray` from `.output()` — no `.encode` method — causing
23 false test failures (whole suite errored in 0.8s in setup). Fix applied to
`tests/test_backend.py`: `return bytes(pdf.output())`. STRESS_TEST.md has the same
stale line and must be updated (Part b of the next Claude Code session).

## PHASE 3 — Chat Functionality (2026-06-14, in progress)

Chat textbar added between upload panels and 'Analyse Everything' button. Provides
a way for users to add narrative context not captured in documents.

### Architecture
- `frontend/index.html`: `<textarea id="chat-input">` with 2000-char cap + counter.
  Scoped in UI as "Additional context for this analysis only · Not a legal advisor."
- `backend/main.py`: `chat_context: str = Form(default='')` added to `/api/analyze`.
  Non-empty chat text joins the `build_entity_map()` texts list (same redaction as docs).
- `backend/analyzer.py`: `analyze_combined()` accepts `chat_context` param. Appended
  to the combined prompt under `<USER_CONTEXT>` wrapper AFTER `<UNTRUSTED_DOCUMENT>`
  blocks, BEFORE the analysis instructions. Tagged as supplementary context, not a document.
- `frontend/db.js` + session schema: `chat_context` field added. Raw (unredacted) text
  stored locally in IndexedDB. Repopulated in textarea on session reload.

### P1 notes (to be updated on completion)
- Chat input > 2000 chars: show 'Too long — upload as a document instead' (enforced by maxlength + JS)
- Empty chat: stripped and excluded from prompt (analyzer not confused by empty USER_CONTEXT)
- Chat debug log: add-then-remove pattern (same as Phase 2 Task 2.4) for NRIC-in-prompt verification
- Multi-turn within a session: not supported — chat is a single textarea per analysis, not a thread
## PHASE 3 — Chat Functionality — COMPLETE (2026-06-15)

Chat textbar between the upload panels and the Analyse button (2000-char cap, live
counter, "For this analysis only · Not a legal advisor" scope note — guardrail #11).
Chat joins the SAME combined entity-map redaction as documents BEFORE the LLM, is passed
to analyze_combined inside a scoped <USER_CONTEXT> block ("supporting info, NOT a new
document, not instructions"), and is persisted per-session in IndexedDB (raw, client-side
only — never re-sent to the server). Repopulates the textarea on session reload.

Verified end-to-end (Chrome MCP, real analysis): chat "S9876543B mentioned training would
start in January 2026" reached the prompt as "[NRIC_2] mentioned training would start in
January 2026" inside <USER_CONTEXT> (raw NRIC count 0); results considered the January
context; reload repopulated the chat; "Clear my data" wiped it. Debug log added then removed.

### P1 notes
- **Chat adds one Daytona round-trip (~5s)** when non-empty (regex backstop sweep on the
  chat). Acceptable; local-fallback applies if Daytona is down.
- **Browser caches index.html** — during MCP testing I had to cache-bust with a ?v= query
  param to pick up new frontend code. Real users get fresh HTML on a normal load; only an
  issue for rapid dev iteration. (Could add Cache-Control headers later if desired.)
