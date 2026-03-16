"""
LLM-powered quiz question generation using Groq API (free, no limits).
Get a free API key at: https://console.groq.com
"""

import os
import json
import re
from groq import Groq

_client = None


def get_client():
    global _client
    if _client is None:
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY environment variable is not set.")
        _client = Groq(api_key=api_key)
    return _client


SYSTEM_PROMPT = """You are an expert educational content creator. Generate high-quality quiz questions from educational text passages.

Always respond with valid JSON only — no markdown fences, no explanation outside the JSON.

Each question object must follow this exact schema:
{
  "question_text": "...",
  "question_type": "mcq" | "true_false" | "fill_in_the_blank",
  "options": ["A. ...", "B. ...", "C. ...", "D. ..."],
  "correct_answer": "...",
  "explanation": "...",
  "difficulty": 1-5,
  "topic_tags": ["tag1", "tag2"]
}

Rules:
- mcq: exactly 4 options labelled A. B. C. D.
- true_false: options must be exactly ["True", "False"]
- fill_in_the_blank: question_text contains ___ as blank; no options field
- Generate equal mix of all three types
"""

GENERATION_PROMPT = """Generate {n} quiz questions from this text passage.
Target difficulty: {difficulty} (1-5 scale).
Include a mix of mcq, true_false, and fill_in_the_blank types.

TEXT:
\"\"\"{chunk_text}\"\"\"

Respond with a JSON array only."""


def _call_groq(prompt: str, system: str = None, max_tokens: int = 2048) -> str:
    client = get_client()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=messages,
        max_tokens=max_tokens,
        temperature=0.7,
    )
    return response.choices[0].message.content.strip()


def generate_questions_for_chunk(
    chunk_text: str,
    n_questions: int = 3,
    target_difficulty: int = 3,
  ) ->list[dict]:
    prompt = GENERATION_PROMPT.format(
        n=n_questions,
        difficulty=target_difficulty,
        chunk_text=chunk_text[:4000],
    )
    raw = _call_groq(prompt, system=SYSTEM_PROMPT, max_tokens=2048)
    questions = _parse_questions(raw)
    return [_validate_question(q) for q in questions]


def regenerate_question(original_question: dict, chunk_text: str, target_difficulty: int) -> dict | None:
    prompt = f"""Rewrite this question at difficulty level {target_difficulty}/5.

ORIGINAL:
{json.dumps(original_question, indent=2)}

SOURCE TEXT:
\"\"\"{chunk_text[:2000]}\"\"\"

Respond with a single JSON question object only."""
    raw = _call_groq(prompt, system=SYSTEM_PROMPT, max_tokens=512)
    questions = _parse_questions(raw)
    return _validate_question(questions[0]) if questions else None


def evaluate_short_answer(question_text: str, correct_answer: str, user_answer: str) -> dict:
    prompt = f"""Grade this student answer.

QUESTION: {question_text}
CORRECT ANSWER: {correct_answer}
STUDENT ANSWER: {user_answer}

Respond with JSON only:
{{"is_correct": true/false, "score": 0.0-1.0, "feedback": "brief explanation"}}"""
    
    try:
        raw = _call_groq(prompt, system="You are a fair grader. Respond with JSON only.", max_tokens=256)
        result = json.loads(_strip_fences(raw))
        return {
            "is_correct": bool(result.get("is_correct", False)),
            "score": float(result.get("score", 0.0)),
            "feedback": str(result.get("feedback", "")),
        }
    except Exception:
        is_correct = user_answer.strip().lower() in correct_answer.strip().lower()
        return {"is_correct": is_correct, "score": 1.0 if is_correct else 0.0, "feedback": ""}


def _call_gemini(prompt: str, system: str = None, max_tokens: int = 256) -> str:
    return _call_groq(prompt, system=system, max_tokens=max_tokens)


def _parse_questions(raw_text: str) -> list[dict]:
    text = _strip_fences(raw_text)
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        text = match.group(0)
    elif text.strip().startswith("{"):
        text = f"[{text}]"
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        return []


def _strip_fences(text: str) -> str:
    return re.sub(r"```(?:json)?\s*(.*?)\s*```", r"\1", text, flags=re.DOTALL).strip()


def _validate_question(q: dict) -> dict:
    q_type = q.get("question_type", "mcq")
    if q_type not in ("mcq", "true_false", "fill_in_the_blank"):
        q_type = "fill_in_the_blank" if q_type == "short_answer" else "mcq"

    difficulty = int(q.get("difficulty", 3))
    difficulty = max(1, min(5, difficulty))

    options = q.get("options")
    if q_type == "true_false" and not options:
        options = ["True", "False"]
    if q_type == "fill_in_the_blank":
        options = None

    question_text = str(q.get("question_text", "")).strip()
    if q_type == "fill_in_the_blank" and "___" not in question_text:
        question_text = question_text.rstrip("?. ") + " ___."

    return {
        "question_text": question_text,
        "question_type": q_type,
        "options": options,
        "correct_answer": str(q.get("correct_answer", "")).strip(),
        "explanation": str(q.get("explanation", "")).strip(),
        "difficulty": difficulty,
        "topic_tags": q.get("topic_tags") or [],
    }