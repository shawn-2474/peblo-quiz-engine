"""
Quiz API routes:
  POST /api/quiz/start            — create a quiz session
  GET  /api/quiz/<session_id>     — get next question (adaptive)
  POST /api/quiz/<session_id>/answer — submit an answer
  GET  /api/quiz/<session_id>/summary — end session & get score report
  GET  /api/quiz/<session_id>/history — all answers in session
"""

from flask import Blueprint, request, jsonify

from database import db, SourceDocument, QuizSession, QuizQuestion, SessionAnswer
from utils.adaptive import select_question, record_answer, session_summary
from utils.llm import evaluate_short_answer

quiz_bp = Blueprint("quiz", __name__)


# -- Fix 3: GET /api/quiz  (topic + difficulty filtering, spec-compliant) ------

@quiz_bp.route("/quiz", methods=["GET"])
def get_questions_filtered():
    """
    Returns quiz questions filtered by topic and/or difficulty.
    Matches the spec example: GET /quiz?topic=shapes&difficulty=easy

    Query params:
      topic       string   partial match against topic_tags or chunk topic
      difficulty  string   easy|medium|hard  OR  int 1-5
      document_id string   optional filter by document
      limit       int      max questions to return (default 10)
      type        string   mcq | true_false | fill_in_the_blank
    """
    topic       = request.args.get("topic",       "").strip().lower()
    difficulty  = request.args.get("difficulty",  "").strip().lower()
    doc_id      = request.args.get("document_id", "").strip()
    q_type      = request.args.get("type",        "").strip()
    limit       = min(int(request.args.get("limit", 10)), 50)

    # Map human-readable difficulty labels to int ranges
    diff_map = {"easy": (1, 2), "medium": (3, 3), "hard": (4, 5)}
    diff_range = None
    if difficulty:
        if difficulty in diff_map:
            diff_range = diff_map[difficulty]
        elif difficulty.isdigit():
            d = int(difficulty)
            diff_range = (d, d)

    query = QuizQuestion.query
    if doc_id:
        query = query.filter(QuizQuestion.document_id == doc_id)
    if diff_range:
        query = query.filter(QuizQuestion.difficulty.between(*diff_range))
    if q_type:
        query = query.filter(QuizQuestion.question_type == q_type)

    questions = query.order_by(QuizQuestion.times_shown.asc()).limit(limit * 3).all()

    # Topic filter in Python (JSONB array contains or chunk topic matches)
    if topic:
        filtered = []
        for q in questions:
            tags = [t.lower() for t in (q.topic_tags or [])]
            chunk_topic = (q.chunk.topic or "").lower() if q.chunk else ""
            if any(topic in t for t in tags) or topic in chunk_topic:
                filtered.append(q)
        questions = filtered

    questions = questions[:limit]

    return jsonify({
        "count":     len(questions),
        "filters":   {"topic": topic, "difficulty": difficulty, "type": q_type},
        "questions": [q.to_dict(include_answer=False) for q in questions],
    })


# ── POST /api/quiz/start ──────────────────────────────────────────────────────

@quiz_bp.route("/quiz/start", methods=["POST"])
def start_quiz():
    """
    Create a new quiz session.

    Body (JSON):
      document_id      string  (required)
      user_id          string  (optional, default "anonymous")
      start_difficulty int     1-5 (optional, default 3)
    """
    data = request.get_json(silent=True) or {}
    doc_id = data.get("document_id")
    if not doc_id:
        return jsonify({"error": "document_id is required"}), 400

    doc = SourceDocument.query.get_or_404(doc_id)
    if doc.status != "ready":
        return jsonify({"error": f"Document is not ready (status: {doc.status})"}), 409

    if not doc.questions:
        return jsonify({"error": "No questions available for this document"}), 409

    start_diff = int(data.get("start_difficulty", 3))
    start_diff = max(1, min(5, start_diff))

    session = QuizSession(
        document_id        = doc_id,
        user_id            = str(data.get("user_id", "anonymous")),
        current_difficulty = start_diff,
    )
    db.session.add(session)
    db.session.commit()

    return jsonify({
        "session_id":         session.id,
        "document_id":        doc_id,
        "document_title":     doc.title,
        "total_questions":    len(doc.questions),
        "start_difficulty":   start_diff,
        "message": "Session started. GET /api/quiz/{session_id} for your first question.",
    }), 201


# ── GET /api/quiz/<session_id> ────────────────────────────────────────────────

@quiz_bp.route("/quiz/<session_id>", methods=["GET"])
def get_question(session_id: str):
    """
    Return the next adaptive question for the session.
    The engine picks difficulty automatically.
    """
    session = QuizSession.query.get_or_404(session_id)

    if session.status == "completed":
        return jsonify({"error": "Session is already completed", "session_id": session_id}), 409

    question = select_question(session, session.document_id)

    if question is None:
        # All questions exhausted — auto-complete
        session.status = "completed"
        db.session.commit()
        summary = session_summary(session)
        return jsonify({
            "message": "All available questions answered. Quiz complete!",
            "summary": summary,
        }), 200

    return jsonify({
        "session_id":          session.id,
        "current_difficulty":  session.current_difficulty,
        "questions_asked":     session.questions_asked,
        "question":            question.to_dict(include_answer=False),
    })


# ── POST /api/quiz/<session_id>/answer ────────────────────────────────────────

@quiz_bp.route("/quiz/<session_id>/answer", methods=["POST"])
def submit_answer(session_id: str):
    """
    Submit an answer and get immediate feedback + adapted difficulty.

    Body (JSON):
      question_id    string   (required)
      answer         string   (required) — option text or free-text
      time_taken_ms  int      (optional)
    """
    session = QuizSession.query.get_or_404(session_id)

    if session.status == "completed":
        return jsonify({"error": "Session is already completed"}), 409

    data = request.get_json(silent=True) or {}
    q_id   = data.get("question_id")
    answer = data.get("answer", "").strip()

    if not q_id or not answer:
        return jsonify({"error": "question_id and answer are required"}), 400

    question = QuizQuestion.query.get_or_404(q_id)

    # Ensure question belongs to this session's document
    if question.document_id != session.document_id:
        return jsonify({"error": "Question does not belong to this session's document"}), 400

    # Check for duplicate answer in this session
    already_answered = SessionAnswer.query.filter_by(
        session_id=session_id, question_id=q_id
    ).first()
    if already_answered:
        return jsonify({"error": "Question already answered in this session"}), 409

    # Grade the answer
    if question.question_type == "fill_in_the_blank":
        # Flexible text match — normalise whitespace and case
        is_correct = (
            user_answer.strip().lower() == question.correct_answer.strip().lower()
        )
        # Fallback: LLM grading for longer fill-in answers
        if not is_correct and len(question.correct_answer.split()) > 2:
            grading    = evaluate_short_answer(
                question_text  = question.question_text,
                correct_answer = question.correct_answer,
                user_answer    = answer,
            )
            is_correct = grading["is_correct"]
            feedback   = grading.get("feedback", "")
        else:
            feedback = ""
    elif question.question_type in ("mcq", "true_false"):
        is_correct = _match_mcq_answer(answer, question.correct_answer)
        feedback   = ""
    else:
        # Legacy short_answer fallback
        grading    = evaluate_short_answer(
            question_text  = question.question_text,
            correct_answer = question.correct_answer,
            user_answer    = answer,
        )
        is_correct = grading["is_correct"]
        feedback   = grading.get("feedback", "")

    result = record_answer(
        session       = session,
        question      = question,
        user_answer   = answer,
        is_correct    = is_correct,
        time_taken_ms = data.get("time_taken_ms"),
    )

    result["llm_feedback"] = feedback
    return jsonify(result)


# ── GET /api/quiz/<session_id>/summary ────────────────────────────────────────

@quiz_bp.route("/quiz/<session_id>/summary", methods=["GET"])
def get_summary(session_id: str):
    """End the session and return a full performance report."""
    session = QuizSession.query.get_or_404(session_id)

    if session.status == "active":
        session.status = "completed"
        db.session.commit()

    return jsonify(session_summary(session))


# ── GET /api/quiz/<session_id>/history ────────────────────────────────────────

@quiz_bp.route("/quiz/<session_id>/history", methods=["GET"])
def get_history(session_id: str):
    """Return all answers in the session with question details."""
    session = QuizSession.query.get_or_404(session_id)
    history = []
    for ans in session.answers:
        q = QuizQuestion.query.get(ans.question_id)
        history.append({
            **ans.to_dict(),
            "question": q.to_dict(include_answer=True) if q else None,
        })
    return jsonify({
        "session_id": session_id,
        "answers": history,
        "summary": session.to_dict(),
    })


# ── Helpers ───────────────────────────────────────────────────────────────────

def _match_mcq_answer(user_answer: str, correct_answer: str) -> bool:
    """
    Flexible MCQ matching:
      "A"            matches "A. Paris"
      "A. Paris"     matches "A. Paris"
      "paris"        matches "A. Paris"  (case-insensitive)
    """
    u = user_answer.strip().lower()
    c = correct_answer.strip().lower()

    if u == c:
        return True
    # User typed just the option letter
    if len(u) == 1 and c.startswith(u + "."):
        return True
    # User typed the option letter + period
    if len(u) == 2 and u.endswith(".") and c.startswith(u):
        return True
    # Core content match (strip leading "A. ", "B. " etc.)
    import re
    c_core = re.sub(r"^[a-d]\.\s*", "", c)
    u_core = re.sub(r"^[a-d]\.\s*", "", u)
    return u_core == c_core
