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
import os
import sys
import json
import uuid
from datetime import datetime
from pathlib import Path

# Make `backend` importable when uvicorn is launched from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

load_dotenv()

from backend.db import get_conn, init_db, migrate_db
from backend.scraper import get_regulations
from backend.extractor import extract_text, extract_context_text
from backend.redaction import redact_documents
from backend.entity_map import build_entity_map, apply_entity_map
from backend.analyzer import analyze_combined
from src.terminal3_signer import sign_report_hash
from backend.security import (
    validate_file, MAX_FILES, MAX_TOTAL_SIZE_BYTES, MAX_FILE_SIZE_BYTES,
)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# ── App + rate limiting (RT4) ────────────────────────────────────────────────
# Configurable so the automated stress suite (which fires many analyze calls in
# seconds) can raise it; production default is 5/minute per IP.
RATE_LIMIT = os.getenv("CLAUSEGUARD_RATE_LIMIT", "5/minute")
limiter = Limiter(key_func=get_remote_address, default_limits=[])
app = FastAPI(title="ClauseGuard", version="2.0")
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=429,
        content={"detail": "Too many requests — please wait a minute and try again."},
    )


app.add_middleware(SlowAPIMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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


@app.get("/health")
async def health():
    return {"status": "ok", "ts": datetime.now().isoformat()}


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
    contract_files: list[UploadFile] = File(default=[]),
    context_files: list[UploadFile] = File(default=[]),
):
    """Dual-panel upload -> ONE combined analysis+judgment LLM call -> persist.

    Panel A (contract_files) is required (employment docs, PDF only). Panel B
    (context_files) is optional (dispute context: PDF/TXT/EML). With no context,
    the judgment comes back INSUFFICIENT_INFORMATION.
    """
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
            raise HTTPException(413, "Total upload exceeds 50MB limit.")
        validate_file(f.filename, content)  # raises HTTPException on violation
        result = extract_text(content, f.filename)
        if result["success"] and result["chars"] > 0:
            contract_docs.append({"filename": f.filename, "text": result["text"]})
        else:
            contract_errors.append({"filename": f.filename, "error": result.get("error", "No text extracted")})

    if not contract_docs:
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
    entity_map = build_entity_map(
        [d["text"] for d in contract_docs] + [d["text"] for d in context_docs]
    )
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
    try:
        combined = analyze_combined(contract_docs, context_docs, regs)
    except TimeoutError:
        raise HTTPException(504, "Analysis timed out. Please try again with fewer or smaller documents.")
    except ValueError as e:
        raise HTTPException(502, detail=f"Analysis returned an unexpected format: {e}")
    except EnvironmentError as e:
        raise HTTPException(500, detail=str(e))
    except Exception as e:
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

    # ── 6. Persist the session (parameterised — RT8). ──────────────────────
    session_id = uuid.uuid4().hex[:8]
    conn = get_conn()
    try:
        conn.execute("""
            INSERT INTO sessions
                (id, created_at, filenames, context_filenames, doc_count, context_doc_count,
                 overall_severity, verdict, analysis, judgment, regulation_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            session_id,
            datetime.now().isoformat(),
            json.dumps([f.filename for f in contract_files]),
            json.dumps([f.filename for f in context_files]),
            len(contract_docs),
            len(context_docs),
            analysis.get("overall_severity", "MODERATE"),
            judgment.get("verdict", "INSUFFICIENT_INFORMATION"),
            json.dumps(analysis),
            json.dumps(judgment),
            reg_data.get("source"),
        ))
        conn.commit()
    finally:
        conn.close()

    return {
        "session_id": session_id,
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
    }


# ── Sessions ─────────────────────────────────────────────────────────────────
@app.get("/api/sessions")
async def sessions():
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT id, created_at, filenames, doc_count, overall_severity, verdict
            FROM sessions ORDER BY created_at DESC LIMIT 30
        """).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["filenames"] = json.loads(d.get("filenames") or "[]")
        out.append({
            "id": d["id"],
            "created_at": d["created_at"],
            "filenames": d["filenames"],
            "overall_severity": d.get("overall_severity"),
            "verdict": d.get("verdict"),  # shown in sidebar
        })
    return out


@app.get("/api/session/{sid}")
async def session(sid: str):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "Session not found.")
    d = dict(row)
    return {
        "id": d["id"],
        "created_at": d["created_at"],
        "filenames": json.loads(d.get("filenames") or "[]"),
        "context_filenames": json.loads(d.get("context_filenames") or "[]"),
        "analysis": json.loads(d.get("analysis") or "{}"),
        "judgment": json.loads(d.get("judgment") or "{}"),  # PM5: was missing
    }


# Mount static assets last so it never shadows the API routes.
if FRONTEND_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
