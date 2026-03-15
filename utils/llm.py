import os
import json
import re
import google.generativeai as genai

_model = None
_grader_model = None


def get_model(system_instruction: str = None):
    """Return a configured Gemini model instance."""
    global _model
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY environment variable is not set. "
            "Get a free key at https://aistudio.google.com"
        )
    genai.configure(api_key=api_key)
    return genai.GenerativeModel(
        model_name="gemini-1.5-flash",   # free tier, fast, generous limits
        system_instruction=system_instruction,
        generation_config=genai.GenerationConfig(
            temperature=0.7,
            max_output_tokens=2048,
        ),
    )


# Keep get_client as an alias so dedup.py import doesn't break
def get_client():
    return get_model()


# ── Prompt templates ───────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert educational content creator. Your task is to generate high-quality quiz questions from educational text passages.

Always respond with valid JSON only — no markdown fences, no explanation text outside the JSON.

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

Question type rules:
- mcq: provide exactly 4 options labelled "A. ...", "B. ...", "C. ...", "D. ..."
- true_false: options must be exactly ["True", "False"]
- fill_in_the_blank: question_text must contain ___ as the blank; correct_answer is the missing word/phrase; no options field

Difficulty guide:
1 = Basic recall of explicitly stated facts
2 = Simple comprehension
3 = Application of concepts
4 = Analysis and inference across the passage
5 = Synthesis, evaluation, or subtle nuance

Generate an equal mix of all three question types unless instructed otherwise.
"""

GENERATION_PROMPT = """Generate {n} quiz questions from the following text passage.
Target difficulty level: {difficulty} (on a 1-5 scale, +/-1 variation is fine).
Include a mix of mcq, true_false, and fill_in_the_blank question types.

TEXT PASSAGE:
\"\"\"{chunk_text}\"\"\"

Respond with a JSON array of question objects only."""


# ── Public API ─────────────────────────────────────────────────────────────────

def generate_questions_for_chunk(
    chunk_text: str,
    n_questions: int = 3,
    target_difficulty: int = 3,
) -> list[dict]:
    """
    Call Gemini to generate quiz questions for a single text chunk.
    Returns a list of validated question dicts (not yet persisted).
    """
    model = get_model(system_instruction=SYSTEM_PROMPT)

    prompt = GENERATION_PROMPT.format(
        n=n_questions,
        difficulty=target_difficulty,
        chunk_text=chunk_text[:4000],
    )

    response = model.generate_content(prompt)
    raw_text = response.text.strip()
    questions = _parse_questions(raw_text)
    return [_validate_question(q) for q in questions]


def regenerate_question(
    original_question: dict,
    chunk_text: str,
    target_difficulty: int,
) -> dict | None:
    """
    Ask Gemini to produce one alternative question at a new difficulty level.
    """
    model = get_model(system_instruction=SYSTEM_PROMPT)

    prompt = f"""Rewrite the following question at difficulty level {target_difficulty}/5.
Keep it based on the same source passage but adjust complexity accordingly.

ORIGINAL QUESTION:
{json.dumps(original_question, indent=2)}

SOURCE PASSAGE (excerpt):
\"\"\"{chunk_text[:2000]}\"\"\"

Respond with a single JSON question object only."""

    response = model.generate_content(prompt)
    raw_text = response.text.strip()
    questions = _parse_questions(raw_text)
    if questions:
        return _validate_question(questions[0])
    return None


def evaluate_short_answer(
    question_text: str,
    correct_answer: str,
    user_answer: str,
) -> dict:
    """
    Use Gemini to grade a fill-in-the-blank or free-text answer.
    Returns {"is_correct": bool, "feedback": str, "score": 0-1}.
    """
    model = get_model(
        system_instruction="You are a fair and concise grader. Respond with JSON only."
    )

    prompt = f"""Grade the following student answer.

QUESTION: {question_text}
CORRECT ANSWER: {correct_answer}
STUDENT ANSWER: {user_answer}

Respond with JSON only:
{{"is_correct": true/false, "score": 0.0-1.0, "feedback": "brief explanation"}}

Be fair — accept paraphrases and partially correct answers with partial scores."""

    try:
        response = model.generate_content(prompt)
        raw = response.text.strip()
        result = json.loads(_strip_fences(raw))
        return {
            "is_correct": bool(result.get("is_correct", False)),
            "score":      float(result.get("score", 0.0)),
            "feedback":   str(result.get("feedback", "")),
        }
    except Exception:
        is_correct = user_answer.strip().lower() in correct_answer.strip().lower()
        return {"is_correct": is_correct, "score": 1.0 if is_correct else 0.0, "feedback": ""}


# ── Internal helpers ───────────────────────────────────────────────────────────

def _call_gemini(prompt: str, system: str = None, max_tokens: int = 256) -> str:
    """Thin wrapper used by dedup.py for simple single-turn calls."""
    model = get_model(system_instruction=system)
    response = model.generate_content(prompt)
    return response.text.strip()


def _parse_questions(raw_text: str) -> list[dict]:
    """Extract a JSON array from the LLM response, tolerating minor noise."""
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
    """Remove ```json ... ``` or ``` ... ``` fences."""
    return re.sub(r"```(?:json)?\s*(.*?)\s*```", r"\1", text, flags=re.DOTALL).strip()


def _validate_question(q: dict) -> dict:
    """Normalise and sanitise a question dict from the LLM."""
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
        "question_text":  question_text,
        "question_type":  q_type,
        "options":        options,
        "correct_answer": str(q.get("correct_answer", "")).strip(),
        "explanation":    str(q.get("explanation", "")).strip(),
        "difficulty":     difficulty,
        "topic_tags":     q.get("topic_tags") or [],
    }
