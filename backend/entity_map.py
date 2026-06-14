"""
backend/entity_map.py
Cross-document entity map for ClauseGuard's redaction-first pipeline.

Runs BEFORE redaction, on ALL of a session's input text COMBINED, so that the
same real-world entity gets ONE consistent placeholder across every file. That
consistency is what lets the analyzer detect cross-document contradictions
("the form signed by [PERSON_1] contradicts [PERSON_1]'s LOA") instead of seeing
the same person as two different anonymous tokens.

Two detection passes, union'd:
  1. Regex (reused verbatim from src/redactor.py) — NRIC, EMAIL, PHONE, ADDRESS.
     Reliable, deterministic.
  2. spaCy en_core_web_sm NER — PERSON, ORG. Catches names in prose / company
     names that regex cannot. Small model: best-effort, occasionally mislabels
     (e.g. a person as ORG) or misses an entity — disclosed in the UI banner.

The map is { "Isabel Lim": "[PERSON_1]", "Xcellink": "[ORG_1]",
"T0174455G": "[NRIC_1]", ... }. It is NEVER sent to the LLM or Bright Data; it is
stored session-locally and used in reverse only when rendering the final report /
MOM letter back to the user (client-side de-redaction).
"""
import re

from src.redactor import _PATTERN_SPECS  # single source of truth for regex

# Regex passes, in a deterministic order (matches src/redactor.py).
_REGEX = [(label, re.compile(pat, flags)) for label, pat, flags in _PATTERN_SPECS]

# Generic words/headings en_core_web_sm frequently mis-tags as PERSON/ORG in
# contract text. Dropping them avoids redacting "Company" everywhere or turning a
# section heading into a fake person. (Best-effort cleanup, not exhaustive.)
_NER_STOPWORDS = {
    "company", "employee", "employer", "designation", "employee particulars",
    "schedule", "contract", "appointment", "salary", "the company",
}


def _is_noise_entity(text: str) -> bool:
    """True if a spaCy PERSON/ORG span is almost certainly a false positive."""
    t = text.strip()
    if len(t) < 2 or len(t) > 40:
        return True
    if "\n" in t or ":" in t:          # spans crossing lines/headings are garbage
        return True
    if t.lower() in _NER_STOPWORDS:    # generic contract vocabulary, not an identity
        return True
    if not any(c.isalpha() for c in t):
        return True
    return False


# spaCy is loaded lazily once (model load is ~0.5s) so importing this module is cheap.
_NLP = None


def _get_nlp():
    global _NLP
    if _NLP is None:
        import spacy
        _NLP = spacy.load("en_core_web_sm")
    return _NLP


def build_entity_map(texts: list) -> dict:
    """Build a {original_value -> placeholder} map across ALL texts combined.

    Placeholders are numbered per type by first appearance: [PERSON_1],
    [PERSON_2], [ORG_1], [NRIC_1], [EMAIL_1], [PHONE_1], [ADDRESS_1] ...
    Because the map is built from the combined text, every text later redacted
    with it gets identical placeholders for identical entities.
    """
    combined = "\n\n".join(t or "" for t in texts)

    emap: dict = {}        # original -> placeholder (insertion-ordered)
    counters: dict = {}    # type -> running count

    def assign(value: str, etype: str) -> None:
        value = (value or "").strip()
        # Skip empties, pure punctuation, and anything already mapped (or itself
        # a placeholder, to stay idempotent).
        if not value or value in emap or value.startswith("["):
            return
        counters[etype] = counters.get(etype, 0) + 1
        emap[value] = f"[{etype}_{counters[etype]}]"

    # 1. Regex entities (deterministic, in order of appearance).
    for label, rx in _REGEX:
        for m in rx.finditer(combined):
            assign(m.group(0), label)

    # 2. NER entities (PERSON / ORG). Best-effort.
    try:
        doc = _get_nlp()(combined)
        for ent in doc.ents:
            if ent.label_ in ("PERSON", "ORG") and not _is_noise_entity(ent.text):
                assign(ent.text, ent.label_)
    except Exception:
        # If spaCy is unavailable, the regex pass still applies — names in prose
        # just won't be caught (the documented best-effort limit).
        pass

    return emap


def apply_entity_map(text: str, emap: dict) -> str:
    """Replace every mapped original with its placeholder.

    Longest originals first so a longer entity (an email, or "Isabel Lim") is
    replaced before any shorter substring of it (avoids leaving fragments).
    """
    if not text or not emap:
        return text
    for original in sorted(emap, key=len, reverse=True):
        text = text.replace(original, emap[original])
    return text


def invert_entity_map(emap: dict) -> dict:
    """{placeholder -> original} for client-side de-redaction of the report."""
    return {placeholder: original for original, placeholder in emap.items()}


if __name__ == "__main__":
    texts = [
        "Isabel Lim is the HR Director. Contact: isabel@xcellinkgroup.com, T0174455G",
        "As confirmed by Isabel Lim in meeting on 28 May 2026...",
    ]
    emap = build_entity_map(texts)
    print("entity_map:", emap)
    print("\nredacted text 1:", apply_entity_map(texts[0], emap))
    print("redacted text 2:", apply_entity_map(texts[1], emap))
    # The actual acceptance criterion: same entity -> same placeholder in both.
    ph = emap.get("Isabel Lim")
    print("\n'Isabel Lim' placeholder:", ph)
    print("consistent across both:",
          ph and ph in apply_entity_map(texts[0], emap)
          and ph in apply_entity_map(texts[1], emap))
