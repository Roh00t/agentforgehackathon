"""
Multi-PDF text extractor using pdfplumber.
Returns structured text per document for downstream analysis.
"""
import io
import pdfplumber


def extract_text(file_bytes: bytes, filename: str) -> dict:
    """
    Extract text from a PDF's bytes.
    Returns: {filename, success, text, pages, chars, error?}
    """
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            page_texts = []
            for i, page in enumerate(pdf.pages):
                text = page.extract_text() or ""
                if text.strip():
                    page_texts.append(f"[Page {i+1}]\n{text.strip()}")

            full_text = "\n\n".join(page_texts)

            if not full_text.strip():
                return {
                    "filename": filename,
                    "success": False,
                    "error": "No extractable text — likely a scanned or image-only PDF.",
                    "text": "",
                    "pages": len(pdf.pages),
                    "chars": 0,
                }

            return {
                "filename": filename,
                "success": True,
                "text": full_text,
                "pages": len(pdf.pages),
                "chars": len(full_text),
            }
    except Exception as e:
        return {
            "filename": filename,
            "success": False,
            "error": str(e),
            "text": "",
            "pages": 0,
            "chars": 0,
        }


def extract_context_text(file_bytes: bytes, filename: str) -> dict:
    """Router for Dispute Context files. Dispatches by extension.

    Accepts PDF (reuses extract_text), plus plain-text .txt/.eml (email threads,
    WhatsApp exports). Returns the same structure as extract_text().
    """
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""

    if ext == "pdf":
        # Validate magic bytes first (RT1) before handing to pdfplumber.
        if file_bytes[:5] != b"%PDF-":
            return {"filename": filename, "success": False,
                    "error": "File does not appear to be a valid PDF.",
                    "text": "", "pages": 0, "chars": 0}
        return extract_text(file_bytes, filename)

    if ext == "txt":
        try:
            text = file_bytes.decode("utf-8", errors="replace").strip()
        except Exception as e:
            return {"filename": filename, "success": False,
                    "error": str(e), "text": "", "pages": 0, "chars": 0}
        if len(text) < 10:
            return {"filename": filename, "success": False,
                    "error": "File appears empty.", "text": "", "pages": 0, "chars": 0}
        # Higher char budget than PDFs — no extraction overhead.
        return {"filename": filename, "success": True,
                "text": text[:12000], "pages": 1, "chars": len(text)}

    if ext == "eml":
        # Parse the email and extract the BODY only (stdlib email, no deps).
        # Useful headers (From/To/Subject/Date) are kept as context — they often
        # carry the dispute's who/when. Any PII in them is entity-mapped +
        # regex-redacted downstream before the analyzer ever sees it.
        try:
            import email
            from email import policy
            import re as _re

            msg = email.message_from_bytes(file_bytes, policy=policy.default)
            part = msg.get_body(preferencelist=("plain", "html"))
            if part is not None:
                body = part.get_content()
                if part.get_content_type() == "text/html":
                    body = _re.sub(r"<[^>]+>", " ", body)          # strip tags
                    body = _re.sub(r"[ \t]*\n[ \t]*", "\n", body)  # tidy whitespace
            elif not msg.is_multipart():
                body = msg.get_content()
            else:
                body = ""

            headers = "\n".join(
                f"{h}: {msg.get(h)}" for h in ("From", "To", "Subject", "Date") if msg.get(h)
            )
            text = ((headers + "\n\n" + (body or "")).strip()) if headers else (body or "").strip()
        except Exception as e:
            return {"filename": filename, "success": False,
                    "error": f"Could not parse .eml: {e}", "text": "", "pages": 0, "chars": 0}

        if len(text) < 10:
            return {"filename": filename, "success": False,
                    "error": "Email body appears empty.", "text": "", "pages": 0, "chars": 0}
        return {"filename": filename, "success": True,
                "text": text[:12000], "pages": 1, "chars": len(text)}

    return {"filename": filename, "success": False,
            "error": f"Unsupported file type '.{ext}'. Context accepts: PDF, TXT, EML.",
            "text": "", "pages": 0, "chars": 0}