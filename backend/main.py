"""
backend/main.py — ClauseGuard v2 FastAPI app.

Routes:
  GET  /                     -> serves the single-page frontend
  GET  /health               -> {"status":"ok"}
  GET  /api/regulations      -> MOM regulation cache status (no full content)
  POST /api/analyze          -> multi-PDF upload -> red-flag analysis JSON
  GET  /api/sessions         -> last 30 analyses (sidebar)
  GET  /api/session/{id}     -> one saved analysis

Run from the repo root (note the package path — backend/__init__.py must exist):
  python3.13 -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
"""
import atexit
import os
import sys
import json
import time
from datetime import datetime, timezone
from pathlib import Path

# Make `backend` importable when uvicorn is launched from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from posthog import Posthog
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

load_dotenv()

# Analytics is disabled when there's no token, or when CLAUSEGUARD_DISABLE_ANALYTICS=1
# (the 34-test suite sets this so test runs never touch the production PostHog
# project or add network latency). Exception autocapture is OFF: uncurated
# tracebacks could carry document text/filenames -> guardrail #13 (metadata only
# to external services). All events are explicit capture() calls with curated props.
_POSTHOG_TOKEN = os.getenv("POSTHOG_PROJECT_TOKEN", "")
_ANALYTICS_DISABLED = (not _POSTHOG_TOKEN) or os.getenv("CLAUSEGUARD_DISABLE_ANALYTICS") == "1"
posthog_client = Posthog(
    api_key=_POSTHOG_TOKEN or "disabled",
    host=os.getenv("POSTHOG_HOST", "https://us.i.posthog.com"),
    disabled=_ANALYTICS_DISABLED,
    enable_exception_autocapture=False,
)
atexit.register(posthog_client.shutdown)

from backend.db import get_conn, init_db, migrate_db
from backend.supabase_client import (
    verify_user_token, get_user_profile, increment_analyses_used,
    log_analysis_metadata, FREE_ANALYSIS_LIMIT,
)
from backend.scraper import get_regulations
from backend.extractor import extract_text, extract_context_text
from backend.redaction import redact_documents
from backend.entity_map import build_entity_map, apply_entity_map
from backend.analyzer import analyze_combined, answer_followup
from backend.report_generator import generate_docx
from src.terminal3_signer import sign_report_hash
from backend.security import (
    validate_file, MAX_FILES, MAX_TOTAL_SIZE_BYTES, MAX_FILE_SIZE_BYTES,
)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# ── App + HYBRID rate limiting (Phase 5) ─────────────────────────────────────
# Per-IP burst limit (slowapi) raised to 20/min: generous for a single user
# behind NAT/retries, not generous enough for abuse. Configurable so the stress
# suite can raise it.
RATE_LIMIT = os.getenv("CLAUSEGUARD_RATE_LIMIT", "20/minute")
limiter = Limiter(key_func=get_remote_address, default_limits=[])
app = FastAPI(title="ClauseGuard", version="2.0")
app.state.limiter = limiter

# Secondary per-session-token gate. Per-IP alone is too restrictive behind NAT;
# per-token alone is bypassable by minting new UUIDs — together they cover both
# vectors. In-memory sliding window (single-process MVP; no Redis). P1: distinct
# tokens accumulate small empty lists over long uptime — fine for MVP.
_SESSION_LIMIT = int(os.getenv("CLAUSEGUARD_SESSION_RATE_LIMIT", "5"))
_SESSION_WINDOW = 60  # seconds
_session_token_counts: dict = {}  # token -> list[timestamp]


def _check_session_rate(token: str, limit: int = _SESSION_LIMIT, window: int = _SESSION_WINDOW) -> bool:
    """True if this token is under its limit (and records the hit); False if over."""
    now = time.time()
    # Fix 6: lazy eviction on each write (no background loop -> no threading
    # issues in FastAPI). Drop any token whose newest request is older than
    # 2x the window, so the dict can't grow unbounded over long uptime.
    stale = [k for k, v in _session_token_counts.items()
             if not v or now - max(v) > window * 2]
    for k in stale:
        del _session_token_counts[k]
    timestamps = [t for t in _session_token_counts.get(token, []) if now - t < window]
    if len(timestamps) >= limit:
        _session_token_counts[token] = timestamps  # keep pruned
        return False
    _session_token_counts[token] = timestamps + [now]
    return True


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    from fastapi.responses import JSONResponse
    posthog_client.capture("anonymous", "rate_limit_exceeded", {"path": request.url.path})
    return JSONResponse(
        status_code=429,
        content={"detail": "Too many requests — please wait a minute and try again."},
    )


app.add_middleware(SlowAPIMiddleware)

# CORS — env-var-driven (ALLOWED_ORIGINS, comma-separated). Defaults to "*" for
# local dev convenience; production sets it to the real Render URL in the
# dashboard. This is the guardrail-#? CORS fix for the Render deploy.
_raw_origins = os.getenv("ALLOWED_ORIGINS", "*")
if _raw_origins.strip() == "*":
    _allowed_origins = ["*"]
else:
    _allowed_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup():
    init_db()
    migrate_db()  # safe on both fresh and existing DBs


# ── Static frontend ──────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/tos")
async def tos():
    """Privacy Policy & Terms (Phase 5). Static page, no user data."""
    return FileResponse(FRONTEND_DIR / "tos.html")


@app.get("/about")
async def about():
    """About page (Phase 9). Static, no user data."""
    return FileResponse(FRONTEND_DIR / "about.html")


@app.get("/pricing")
async def pricing():
    """Pricing page (Phase 9). Static; checkout disabled (Stripe deferred)."""
    return FileResponse(FRONTEND_DIR / "pricing.html")


@app.get("/support")
async def support():
    """Support page (Phase 9). Static, no user data."""
    return FileResponse(FRONTEND_DIR / "support.html")


@app.get("/health")
async def health():
    return {"status": "ok", "ts": datetime.now().isoformat()}


# ── Public config (v3 B) ─────────────────────────────────────────────────────
@app.get("/api/config")
async def public_config():
    """Returns public (non-secret) config the browser needs — Supabase URL + anon key.
    The anon/publishable key is designed to be public; it enforces RLS on the DB side."""
    return {
        "supabase_url": os.getenv("SUPABASE_URL", ""),
        "supabase_anon_key": os.getenv("SUPABASE_PUBLISHABLE_KEY", ""),
        "posthog_token": os.getenv("POSTHOG_PROJECT_TOKEN", ""),
        "posthog_host": os.getenv("POSTHOG_HOST", "https://us.i.posthog.com"),
    }


# ── Current user (v3 C) ──────────────────────────────────────────────────────
@app.get("/api/me")
async def me(request: Request):
    """Return the logged-in user's tier + usage so the frontend can gate UI.
    401 if no/invalid token. Metadata only — no document content (guardrail #13)."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(401, "Not authenticated.")
    user = verify_user_token(auth_header[7:].strip())
    if not user:
        raise HTTPException(401, "Invalid or expired session.")
    profile = get_user_profile(user["user_id"]) or {}
    return {
        "email": user["email"],
        "tier": profile.get("tier", "free"),
        "analyses_used": profile.get("analyses_used", 0),
        "limit": FREE_ANALYSIS_LIMIT,
    }


# ── Regulations ──────────────────────────────────────────────────────────────
@app.get("/api/regulations")
async def regulations():
    """MOM regulation cache status. Full content is stripped (LLM-only)."""
    try:
        result = get_regulations()
        for r in result.get("regulations", []):
            r.pop("content", None)
        return result
    except Exception as e:
        raise HTTPException(500, detail=str(e))


# ── Analyze ──────────────────────────────────────────────────────────────────
@app.post("/api/analyze")
@limiter.limit(RATE_LIMIT)
async def analyze(
    request: Request,
    response: Response,
    contract_files: list[UploadFile] = File(default=[]),
    context_files: list[UploadFile] = File(default=[]),
    chat_context: str = Form(default=""),
    mode: str = Form(default="dispute"),
):
    """Dual-panel upload -> ONE combined analysis+judgment LLM call -> persist.

    Panel A (contract_files) is required (employment docs, PDF only). Panel B
    (context_files) is optional (dispute context: PDF/TXT/EML). With no context,
    the judgment comes back INSUFFICIENT_INFORMATION.
    """
    # ── 0a. Per-session-token rate gate (Phase 5 hybrid). ──────────────────────
    session_token = request.headers.get("X-Session-Token")
    if session_token and not _check_session_rate(session_token):
        posthog_client.capture("anonymous", "rate_limit_exceeded", {"path": "/api/analyze", "gate": "session"})
        raise HTTPException(429, "Too many requests for this session — please wait a minute and try again.")

    # ── 0b. Optional Supabase auth (v3 Part B). ────────────────────────────────
    # Anonymous requests pass through with NO backend cap (guardrail #14 —
    # anonymous free-tier cap is frontend-only via localStorage).
    # Logged-in requests are verified and free-tier is enforced server-side.
    _auth_user = None   # {user_id, email} if verified, else None
    _auth_tier = None   # 'free' | 'paid' | None
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        jwt_token = auth_header[7:].strip()
        _auth_user = verify_user_token(jwt_token)
        if _auth_user:
            profile = get_user_profile(_auth_user["user_id"])
            _auth_tier = (profile or {}).get("tier", "free")
            used = (profile or {}).get("analyses_used", 0)
            if _auth_tier == "free" and used >= FREE_ANALYSIS_LIMIT:
                posthog_client.capture(
                    _auth_user["user_id"],
                    "free_tier_limit_reached",
                    {"analyses_used": used, "limit": FREE_ANALYSIS_LIMIT},
                )
                raise HTTPException(403, detail={
                    "code": "FREE_LIMIT_REACHED",
                    "message": f"Free tier limit of {FREE_ANALYSIS_LIMIT} analyses reached. Upgrade to continue.",
                    "analyses_used": used,
                    "limit": FREE_ANALYSIS_LIMIT,
                })

    # ── 1. Validate counts (combined total — PM13/RT4) ─────────────────────
    total_files = len(contract_files) + len(context_files)
    if total_files == 0:
        raise HTTPException(400, "No files uploaded. Please select at least one employment document.")
    if len(contract_files) == 0:
        raise HTTPException(400, "At least one employment document is required in the Employment Documents panel.")
    if total_files > MAX_FILES:
        raise HTTPException(400, f"Maximum {MAX_FILES} files total ({total_files} submitted). Please reduce your selection.")

    # ── 2. Read + validate contract files (PDF only). Pass RAW text; the
    #       analyzer does the single untrusted-data wrapping + dedup. ────────
    contract_docs = []
    contract_errors = []
    total_bytes = 0
    for f in contract_files:
        content = await f.read()
        total_bytes += len(content)
        if total_bytes > MAX_TOTAL_SIZE_BYTES:
            posthog_client.capture(
                _auth_user["user_id"] if _auth_user else "anonymous",
                "upload_size_exceeded",
                {"total_bytes": total_bytes, "limit_bytes": MAX_TOTAL_SIZE_BYTES},
            )
            raise HTTPException(413, "Total upload exceeds 50MB limit.")
        validate_file(f.filename, content)  # raises HTTPException on violation
        result = extract_text(content, f.filename)
        if result["success"] and result["chars"] > 0:
            contract_docs.append({"filename": f.filename, "text": result["text"]})
        else:
            contract_errors.append({"filename": f.filename, "error": result.get("error", "No text extracted")})

    if not contract_docs:
        posthog_client.capture(
            _auth_user["user_id"] if _auth_user else "anonymous",
            "analysis_file_rejected",
            {"files_count": len(contract_files), "error_count": len(contract_errors)},
        )
        raise HTTPException(422, detail={
            "message": "No text could be extracted from any employment document.",
            "tip": "Ensure your PDFs are text-based (not scanned images). Scanned documents require OCR first.",
            "errors": contract_errors,
        })

    # ── 3. Read + validate context files (PDF/TXT/EML). Empty is valid. ─────
    context_docs = []
    context_errors = []
    for f in context_files:
        content = await f.read()
        total_bytes += len(content)
        if total_bytes > MAX_TOTAL_SIZE_BYTES:
            raise HTTPException(413, "Total upload exceeds 50MB limit.")
        if len(content) > MAX_FILE_SIZE_BYTES:
            context_errors.append({"filename": f.filename, "error": f"Exceeds {MAX_FILE_SIZE_BYTES // 1024 // 1024}MB limit"})
            continue
        result = extract_context_text(content, f.filename)  # filters empty/zero-text (PM9)
        if result["success"] and result["chars"] > 0:
            context_docs.append({"filename": f.filename, "text": result["text"]})
        else:
            context_errors.append({"filename": f.filename, "error": result.get("error", "No text extracted")})

    # ── 3b. CROSS-DOCUMENT ENTITY MAP (NER + regex) over ALL extracted text
    #        COMBINED, applied BEFORE the regex sweep and the LLM. The same real
    #        entity gets the SAME placeholder in every file ([PERSON_1], [ORG_1],
    #        [NRIC_1] ...), which is what lets the analyzer catch cross-document
    #        contradictions. The map holds real PII -> placeholder; it is returned
    #        to the browser for client-side de-redaction ONLY, never persisted and
    #        never sent to the LLM/Bright Data (guardrail #2/#3). ────────────────
    #        Phase 3: the chat context (if any) joins this combined build too,
    #        so a person named in both a document and the chat gets the SAME
    #        placeholder — and chat PII is redacted before the LLM (guardrail #2).
    all_texts = [d["text"] for d in contract_docs] + [d["text"] for d in context_docs]
    if chat_context.strip():
        all_texts.append(chat_context)
    entity_map = build_entity_map(all_texts)
    for d in contract_docs:
        d["text"] = apply_entity_map(d["text"], entity_map)
    for d in context_docs:
        d["text"] = apply_entity_map(d["text"], entity_map)

    # Entity-type COUNTS for the UI banner (counts only — never the values).
    entity_counts: dict = {}
    for placeholder in entity_map.values():
        etype = placeholder.strip("[]").rsplit("_", 1)[0]
        entity_counts[etype] = entity_counts.get(etype, 0) + 1

    # ── 3c. Second sweep: Daytona/local regex redactor as a backstop for any
    #        NRIC/email/phone/address the entity map missed. The LLM still never
    #        receives un-redacted text (redact -> wrap -> analyze). ──────────────
    contract_docs = redact_documents(contract_docs)
    context_docs = redact_documents(context_docs)

    # Phase 3: redact the chat context through the SAME pipeline (entity map +
    # regex backstop). Only include it if non-empty after stripping (RT: empty
    # chat must not produce a confusing empty USER_CONTEXT section).
    redacted_chat = ""
    if chat_context.strip():
        chat_mapped = apply_entity_map(chat_context, entity_map)
        redacted_chat = redact_documents(
            [{"filename": "chat_context", "text": chat_mapped}]
        )[0]["text"]

    redaction_reports = [
        {
            "filename": d["filename"],
            "panel": panel,
            "redaction_report": d["redaction_report"],
            "total_redactions": d["total_redactions"],
            "engine": d["engine"],
        }
        for panel, docs in (("contract", contract_docs), ("context", context_docs))
        for d in docs
    ]

    # ── 4. Load MOM regulations (cached, fast after first scrape). ──────────
    reg_data = get_regulations()
    regs = reg_data.get("regulations", [])

    # ── 5. ONE combined LLM call. Timeouts -> 504, never a frozen spinner. ──
    _distinct_id = _auth_user["user_id"] if _auth_user else "anonymous"
    try:
        combined = analyze_combined(contract_docs, context_docs, regs, chat_context=redacted_chat, mode=mode)
    except TimeoutError:
        posthog_client.capture(_distinct_id, "analysis_failed", {"mode": mode, "reason": "timeout"})
        raise HTTPException(504, "Analysis timed out. Please try again with fewer or smaller documents.")
    except ValueError as e:
        posthog_client.capture(_distinct_id, "analysis_failed", {"mode": mode, "reason": "invalid_json"})
        raise HTTPException(502, detail=f"Analysis returned an unexpected format: {e}")
    except EnvironmentError as e:
        posthog_client.capture(_distinct_id, "analysis_failed", {"mode": mode, "reason": "env_error"})
        raise HTTPException(500, detail=str(e))
    except Exception as e:
        posthog_client.capture(_distinct_id, "analysis_failed", {"mode": mode, "reason": "unknown"})
        raise HTTPException(500, detail=f"Analysis error: {e}")

    analysis = combined["analysis"]
    judgment = combined["judgment"]
    duplicate_warnings = combined.get("duplicate_warnings", [])

    # ── 5b. Tamper-evident attestation (Addition B). Hash the final report and
    #        HMAC-sign it via Terminal 3 (pure stdlib, no network -> cannot fail
    #        due to connectivity). Proves the report is UNALTERED since signing,
    #        NOT that the analysis is correct. ──────────────────────────────────
    import hashlib
    report_bytes = json.dumps(
        {"analysis": analysis, "judgment": judgment}, sort_keys=True
    ).encode("utf-8")
    report_hash = hashlib.sha256(report_bytes).hexdigest()
    try:
        attestation = sign_report_hash(report_hash)
    except Exception as e:  # noqa: BLE001 -- attestation must never block analysis
        attestation = {"error": f"signing unavailable: {e}", "report_hash": report_hash}

    # ── 5c. v3 auth: increment counter + log metadata for logged-in users. ────
    # Only runs after a successful analysis. Failure must never block the response.
    if _auth_user:
        increment_analyses_used(_auth_user["user_id"])
        log_analysis_metadata(
            user_id=_auth_user["user_id"],
            mode=mode,
            verdict_category=judgment.get("verdict"),
            docs_count=len(contract_docs) + len(context_docs),
        )
        posthog_client.set(
            distinct_id=_auth_user["user_id"],
            properties={"tier": _auth_tier},
        )

    # ── 5d. PostHog: capture analysis_completed. ─────────────────────────────
    posthog_client.capture(
        _distinct_id,
        "analysis_completed",
        {
            "mode": mode,
            "verdict": judgment.get("verdict"),
            "overall_severity": analysis.get("overall_severity"),
            "contract_docs_count": len(contract_docs),
            "context_docs_count": len(context_docs),
            "has_chat_context": bool(chat_context.strip()),
            "auth_tier": _auth_tier or "anonymous",
        },
    )

    # ── 6. PHASE 2: NO server-side session persistence. ─────────────────────
    # The session (analysis results, entity map, etc.) is stored client-side in
    # the browser's IndexedDB — the server is stateless w.r.t. user content
    # ("we don't have your data"). The client generates its own UUID for storage;
    # Fix 4 removed the vestigial server-minted session_id from the response.
    # The X-Session-Storage header tells the frontend to persist the result.
    # (regulations and scrape_log tables are server data and are still written.)
    response.headers["X-Session-Storage"] = "client"

    return {
        "docs_processed": len(contract_docs),
        "context_docs_processed": len(context_docs),
        "extraction_errors": contract_errors + context_errors,
        "duplicate_files_excluded": duplicate_warnings,
        "redaction_reports": redaction_reports,
        "entity_map": entity_map,          # {real -> placeholder}; browser-only de-redaction
        "entity_counts": entity_counts,    # {type -> count}; for the banner (no values)
        "attestation": attestation,
        "regulation_source": reg_data.get("source"),
        "analysis": analysis,
        "judgment": judgment,
        # v3: auth metadata (email only — never PII from documents)
        "auth": {"email": _auth_user["email"], "tier": _auth_tier} if _auth_user else None,
    }


# ── Download report (Phase 4) ────────────────────────────────────────────────
@app.post("/api/download")
async def download_report(request: Request):
    """Generate a DOCX evidence pack from a completed analysis. Stateless:
    the client sends the analysis JSON + reversed entity map (placeholder->real);
    we de-redact, build the DOCX, stream it back, and store NOTHING (guardrail #3).
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body.")

    report = body.get("analysis") or {}            # the full /api/analyze response
    emap_reversed = body.get("entity_map_reversed") or {}  # placeholder -> real
    filenames = body.get("filenames") or []
    if not report:
        raise HTTPException(400, "No analysis provided to render.")

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        docx_bytes = generate_docx(report, emap_reversed, filenames, generated_at)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, detail=f"Report generation failed: {e}")

    # Attribute to the logged-in user when a valid token is present (else anonymous).
    _dl_auth = verify_user_token(request.headers.get("Authorization", "")[7:].strip()) \
        if request.headers.get("Authorization", "").startswith("Bearer ") else None
    posthog_client.capture(
        _dl_auth["user_id"] if _dl_auth else "anonymous",
        "report_downloaded",
        {"filenames_count": len(filenames), "verdict": (body.get("judgment") or {}).get("verdict")},
    )

    filename = f"ClauseGuard_Report_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.docx"
    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Follow-up chat (v3 Part A6) ──────────────────────────────────────────────
@app.post("/api/chat-followup")
@limiter.limit(RATE_LIMIT)
async def chat_followup(request: Request):
    """Stateless follow-up Q&A after a completed analysis.

    The client sends the redacted-question-safe summary + the employee's raw
    question. The question is redacted through the SAME regex backstop used for
    chat_context in /api/analyze before reaching the LLM.
    """
    session_token = request.headers.get("X-Session-Token")
    if session_token and not _check_session_rate(session_token):
        raise HTTPException(429, "Too many requests for this session — please wait a minute.")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body.")

    question_raw = (body.get("question") or "").strip()
    context_summary = (body.get("context_summary") or "").strip()

    if not question_raw:
        raise HTTPException(400, "Question is required.")
    if len(question_raw) > 1000:
        raise HTTPException(400, "Question must be 1000 characters or fewer.")

    # Redact the question through the SAME pipeline as Phase 3 chat_context:
    # the entity map is NOT available server-side for a stateless call, so we
    # apply only the regex backstop (Pass 1 — NRIC/email/phone/address). This
    # is the same backstop used in the analyze endpoint's 3c step.
    from backend.redaction import redact_documents
    redacted_result = redact_documents([{"filename": "question", "text": question_raw}])
    redacted_question = redacted_result[0]["text"]

    try:
        answer = answer_followup(redacted_question, context_summary)
    except TimeoutError:
        raise HTTPException(504, "Follow-up timed out — please try again.")
    except Exception as e:
        raise HTTPException(500, detail=f"Follow-up error: {e}")

    _cf_auth = verify_user_token(request.headers.get("Authorization", "")[7:].strip()) \
        if request.headers.get("Authorization", "").startswith("Bearer ") else None
    posthog_client.capture(
        _cf_auth["user_id"] if _cf_auth else "anonymous",
        "chat_followup_asked",
        {"question_length": len(question_raw), "has_context": bool(context_summary)},
    )
    return {"answer": answer}


# ── Sessions (DEPRECATED — Phase 2) ──────────────────────────────────────────
# Sessions now live in the browser's IndexedDB. These read endpoints are kept
# (not deleted) so any old/cached client gets a clear 410 Gone instead of a 404
# or stale server data. The sessions table is no longer written to.
_SESSIONS_GONE = {
    "error": "Sessions are now stored in your browser only. "
             "Re-run the analysis to see results."
}


@app.get("/api/sessions")
async def sessions():
    return JSONResponse(status_code=410, content=_SESSIONS_GONE)


@app.get("/api/session/{sid}")
async def session(sid: str):
    return JSONResponse(status_code=410, content=_SESSIONS_GONE)


# Mount static assets last so it never shadows the API routes.
if FRONTEND_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
