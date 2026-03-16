"""
PDF extraction and text chunking utilities.
Uses pdfplumber for robust text + layout extraction.
"""

import re
import math
import pdfplumber


# ── Constants ──────────────────────────────────────────────────────────────────

CHUNK_TARGET_TOKENS  = 100   # target chunk size (rough token estimate)
CHUNK_OVERLAP_TOKENS = 20    # overlap between adjacent chunks
WORDS_PER_TOKEN      = 0.75  # rough approximation


# ── PDF Extraction ─────────────────────────────────────────────────────────────

def extract_pdf(file_path: str) -> dict:
    """
    Extract text from a PDF file.

    Returns:
        {
          "pages": [{"page_num": int, "text": str}, ...],
          "full_text": str,
          "page_count": int,
          "title": str | None,
        }
    """
    pages = []
    metadata_title = None

    with pdfplumber.open(file_path) as pdf:
        page_count = len(pdf.pages)

        if pdf.metadata:
            metadata_title = pdf.metadata.get("Title") or pdf.metadata.get("title")

        for i, page in enumerate(pdf.pages):
            raw = page.extract_text(x_tolerance=3, y_tolerance=3)
            if raw:
                cleaned = _clean_page_text(raw)
                pages.append({"page_num": i + 1, "text": cleaned})

    full_text = "\n\n".join(p["text"] for p in pages)

    return {
        "pages": pages,
        "full_text": full_text,
        "page_count": page_count,
        "title": metadata_title,
    }


def _clean_page_text(text: str) -> str:
    """Remove noise from raw PDF text."""
    # Collapse multiple spaces / tabs
    text = re.sub(r"[ \t]+", " ", text)
    # Normalise newlines
    text = re.sub(r"\r\n?", "\n", text)
    # Remove lines that are only punctuation / page numbers
    lines = [l.strip() for l in text.split("\n")]
    lines = [l for l in lines if len(l) > 3 and not re.fullmatch(r"[\d\s\-–—|]+", l)]
    # Collapse 3+ blank lines into 2
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return text.strip()


# ── Chunking ───────────────────────────────────────────────────────────────────

def chunk_text(pages: list[dict]) -> list[dict]:
    """
    Split page list into overlapping chunks suitable for LLM question generation.

    Each chunk dict:
        {
          "chunk_index": int,
          "text": str,
          "page_start": int,
          "page_end": int,
          "token_count": int,
        }
    """
    # Build a flat list of sentences with their source page
    sentences = []
    for page in pages:
        for sent in _split_sentences(page["text"]):
            if sent.strip():
                sentences.append({"text": sent.strip(), "page": page["page_num"]})

    if not sentences:
        return []

    chunks       = []
    chunk_idx    = 0
    start        = 0
    target_words = int(CHUNK_TARGET_TOKENS / WORDS_PER_TOKEN)
    overlap_words = int(CHUNK_OVERLAP_TOKENS / WORDS_PER_TOKEN)

    while start < len(sentences):
        word_count = 0
        end = start
        while end < len(sentences) and word_count < target_words:
            word_count += len(sentences[end]["text"].split())
            end += 1

        chunk_sents  = sentences[start:end]
        chunk_text   = " ".join(s["text"] for s in chunk_sents)
        token_est    = math.ceil(len(chunk_text.split()) * WORDS_PER_TOKEN)

        chunks.append({
            "chunk_index": chunk_idx,
            "text":        chunk_text,
            "page_start":  chunk_sents[0]["page"],
            "page_end":    chunk_sents[-1]["page"],
            "token_count": token_est,
        })

        chunk_idx += 1

        # Step forward with overlap
        overlap_wc = 0
        back = end - 1
        while back > start and overlap_wc < overlap_words:
            overlap_wc += len(sentences[back]["text"].split())
            back -= 1
        start = max(back, start + 1)   # guarantee forward progress

    return chunks


def _split_sentences(text: str) -> list[str]:
    """Naive but fast sentence splitter (avoids NLTK dependency for speed)."""
    # Split on '. ', '! ', '? ' followed by a capital letter or end of string
    raw = re.split(r"(?<=[.!?])\s+(?=[A-Z\"])", text)
    # Also split on double-newline paragraph boundaries
    result = []
    for seg in raw:
        result.extend(seg.split("\n\n"))
    return [s.strip() for s in result if s.strip()]
