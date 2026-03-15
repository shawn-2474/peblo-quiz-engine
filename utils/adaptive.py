"""
Adaptive Difficulty Engine

Adjusts question difficulty based on the learner's recent performance
using an ELO-inspired rolling window algorithm.
"""

from database import db, QuizSession, QuizQuestion, SessionAnswer

# ── Tuning constants ──────────────────────────────────────────────────────────
WINDOW_SIZE         = 5     # number of recent answers to consider
TARGET_ACCURACY     = 0.70  # ideal accuracy — 70% correct keeps challenge high
DIFFICULTY_MIN      = 1
DIFFICULTY_MAX      = 5
UP_THRESHOLD        = 0.80  # accuracy above this → increase difficulty
DOWN_THRESHOLD      = 0.50  # accuracy below this → decrease difficulty


# ── Public API ────────────────────────────────────────────────────────────────

def next_difficulty(session: QuizSession) -> int:
    """
    Compute the recommended difficulty for the next question in a session.
    Uses a rolling window of recent answers.
    """
    recent_answers = (
        SessionAnswer.query
        .filter_by(session_id=session.id)
        .order_by(SessionAnswer.answered_at.desc())
        .limit(WINDOW_SIZE)
        .all()
    )

    if not recent_answers:
        return session.current_difficulty

    recent_correct = sum(1 for a in recent_answers if a.is_correct)
    accuracy       = recent_correct / len(recent_answers)
    current        = session.current_difficulty

    if accuracy >= UP_THRESHOLD:
        new_difficulty = min(current + 1, DIFFICULTY_MAX)
    elif accuracy <= DOWN_THRESHOLD:
        new_difficulty = max(current - 1, DIFFICULTY_MIN)
    else:
        new_difficulty = current

    return new_difficulty


def select_question(session: QuizSession, document_id: str) -> QuizQuestion | None:
    """
    Pick the best next question for a session:
    1. Match the session's current difficulty (±1 fallback).
    2. Exclude questions already answered in this session.
    3. Prefer questions with fewer prior exposures (underused questions first).
    """
    answered_ids = {a.question_id for a in session.answers}

    target   = session.current_difficulty
    for spread in (0, 1, 2, DIFFICULTY_MAX):          # widen search if needed
        lo = max(DIFFICULTY_MIN, target - spread)
        hi = min(DIFFICULTY_MAX, target + spread)

        candidates = (
            QuizQuestion.query
            .filter(
                QuizQuestion.document_id == document_id,
                QuizQuestion.difficulty.between(lo, hi),
                ~QuizQuestion.id.in_(answered_ids) if answered_ids else db.true(),
            )
            .order_by(QuizQuestion.times_shown.asc())
            .limit(10)
            .all()
        )

        if candidates:
            # Among least-shown candidates, prefer the one closest to target difficulty
            candidates.sort(key=lambda q: (q.times_shown, abs(q.difficulty - target)))
            return candidates[0]

    return None


def record_answer(
    session:    QuizSession,
    question:   QuizQuestion,
    user_answer: str,
    is_correct:  bool,
    time_taken_ms: int | None = None,
) -> dict:
    """
    Persist an answer, update session stats, adapt difficulty, and return
    a result summary.
    """
    answer = SessionAnswer(
        session_id        = session.id,
        question_id       = question.id,
        user_answer       = user_answer,
        is_correct        = is_correct,
        time_taken_ms     = time_taken_ms,
        difficulty_at_time = session.current_difficulty,
    )
    db.session.add(answer)

    # Update question stats
    question.times_shown   += 1
    question.times_correct += (1 if is_correct else 0)

    # Update session stats
    session.questions_asked  += 1
    if is_correct:
        session.questions_correct += 1

    # Adapt difficulty for next question
    new_diff                  = next_difficulty(session)
    session.current_difficulty = new_diff

    db.session.commit()

    return {
        "is_correct":          is_correct,
        "correct_answer":      question.correct_answer,
        "explanation":         question.explanation,
        "new_difficulty":      new_diff,
        "session_score_pct":   session.score_pct,
        "questions_asked":     session.questions_asked,
    }


def session_summary(session: QuizSession) -> dict:
    """Generate a final performance summary for a completed session."""
    answers = session.answers
    by_difficulty = {}
    for ans in answers:
        d = str(ans.difficulty_at_time)
        if d not in by_difficulty:
            by_difficulty[d] = {"shown": 0, "correct": 0}
        by_difficulty[d]["shown"]   += 1
        by_difficulty[d]["correct"] += int(ans.is_correct)

    accuracy_by_diff = {
        d: round(v["correct"] / v["shown"] * 100, 1)
        for d, v in by_difficulty.items()
    }

    return {
        "session_id":         session.id,
        "total_questions":    session.questions_asked,
        "total_correct":      session.questions_correct,
        "overall_score_pct":  session.score_pct,
        "final_difficulty":   session.current_difficulty,
        "accuracy_by_difficulty": accuracy_by_diff,
        "performance_band": _performance_band(session.score_pct),
    }


def _performance_band(score_pct: float) -> str:
    if score_pct >= 85:  return "excellent"
    if score_pct >= 70:  return "proficient"
    if score_pct >= 50:  return "developing"
    return "needs_review"
