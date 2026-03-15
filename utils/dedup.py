"""
Duplicate question detection + question quality validation.

Two-stage duplicate detection:
  1. Fast: normalised string similarity (no API call)
  2. Slow: LLM semantic check (only when similarity is in uncertain band)
"""

import re
import json
from difflib import SequenceMatcher

EXACT_DUP_THRESHOLD    = 0.92
SEMANTIC_CHECK_TRIGGER = 0.70
MIN_QUALITY_SCORE      = 0.60


# ── Public API ─────────────────────────────────────────────────────────────────

def is_duplicate(
    new_question: str,
    existing_questions: list[str],
    use_llm: bool = True,
) -> tuple[bool, str]:
    """
    Check whether new_question is a duplicate of any in existing_questions.
    Returns (is_dup: bool, reason: str).
    """
    norm_new   = _normalise(new_question)
    best_ratio = 0.0
    best_match = ""

    for existing in existing_questions:
        ratio = SequenceMatcher(None, norm_new, _normalise(existing)).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = existing

    if best_ratio >= EXACT_DUP_THRESHOLD:
        return True, f"Near-identical to existing question (similarity {best_ratio:.2f})"

    if use_llm and best_ratio >= SEMANTIC_CHECK_TRIGGER and best_match:
        return _llm_semantic_check(new_question, best_match)

    return False, ""


def validate_question_quality(question: dict) -> tuple[bool, float, str]:
    """
    Score a question 0.0-1.0 for educational quality using Gemini.
    Returns (passes: bool, score: float, feedback: str).
    """
    try:
        from utils.llm import _call_gemini

        prompt = f"""Evaluate the quality of this quiz question for educational use.

QUESTION:
{json.dumps(question, indent=2)}

Score it from 0.0 to 1.0. Respond with JSON only:
{{"score": 0.0-1.0, "passes": true/false, "feedback": "one sentence"}}

Scoring guide:
- 0.9-1.0: Excellent
- 0.7-0.8: Good
- 0.5-0.6: Marginal
- 0.0-0.4: Reject (trivial, malformed, or wrong answer)

Set passes=true if score >= 0.6."""

        raw = _call_gemini(
            prompt,
            system="You are an educational question quality evaluator. Respond with JSON only.",
            max_tokens=128,
        )
        raw = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()
        result = json.loads(raw)

        score    = float(result.get("score", 0.0))
        passes   = bool(result.get("passes", score >= MIN_QUALITY_SCORE))
        feedback = str(result.get("feedback", ""))
        return passes, score, feedback

    except Exception as e:
        return True, 1.0, f"Quality check skipped: {e}"


def filter_duplicates_and_validate(
    new_questions: list[dict],
    existing_question_texts: list[str],
    run_quality_check: bool = True,
) -> tuple[list[dict], list[dict]]:
    """
    Deduplicate and quality-check a batch of new questions.
    Returns (accepted, rejected).
    """
    accepted = []
    rejected = []
    seen_in_batch: list[str] = list(existing_question_texts)

    for q in new_questions:
        q_text = q.get("question_text", "")

        is_dup, dup_reason = is_duplicate(q_text, seen_in_batch, use_llm=True)
        if is_dup:
            rejected.append({**q, "rejection_reason": f"Duplicate: {dup_reason}"})
            continue

        if run_quality_check:
            passes, score, feedback = validate_question_quality(q)
            if not passes:
                rejected.append({
                    **q,
                    "rejection_reason": f"Quality too low (score {score:.2f}): {feedback}",
                })
                continue
            q = {**q, "quality_score": round(score, 2)}

        accepted.append(q)
        seen_in_batch.append(q_text)

    return accepted, rejected


# ── Internal helpers ───────────────────────────────────────────────────────────

def _normalise(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _llm_semantic_check(question_a: str, question_b: str) -> tuple[bool, str]:
    """Ask Gemini whether two questions are semantically equivalent."""
    try:
        from utils.llm import _call_gemini

        prompt = f"""Are these two quiz questions asking the same thing, even if worded differently?

Question A: {question_a}
Question B: {question_b}

Respond with JSON only: {{"is_duplicate": true/false, "reason": "one sentence"}}"""

        raw = _call_gemini(
            prompt,
            system="You are a duplicate question detector. Respond with JSON only.",
            max_tokens=80,
        )
        raw = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()
        result = json.loads(raw)
        return bool(result.get("is_duplicate", False)), str(result.get("reason", ""))

    except Exception:
        return False, ""
