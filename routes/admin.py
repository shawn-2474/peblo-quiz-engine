"""
Admin / management routes:
  GET    /api/admin/documents/<id>/questions        -- list all questions for a doc
  GET    /api/admin/documents/<id>/chunks           -- list all chunks
  DELETE /api/admin/documents/<id>                  -- delete a document + all data
  GET    /api/admin/sessions                        -- list all sessions
  GET    /api/admin/stats                           -- aggregate stats
  POST   /api/admin/documents/<id>/check-duplicates -- run dedup scan on existing questions
  DELETE /api/admin/questions/<id>                  -- delete a single question
"""

from flask import Blueprint, request, jsonify
from database import db, SourceDocument, ContentChunk, QuizQuestion, QuizSession
from utils.dedup import is_duplicate

admin_bp = Blueprint("admin", __name__)


@admin_bp.route("/documents/<doc_id>/questions", methods=["GET"])
def list_questions(doc_id: str):
    doc        = SourceDocument.query.get_or_404(doc_id)
    difficulty = request.args.get("difficulty", type=int)
    q_type     = request.args.get("type")
    page       = request.args.get("page", 1, type=int)
    per_page   = request.args.get("per_page", 20, type=int)

    query = QuizQuestion.query.filter_by(document_id=doc_id)
    if difficulty:
        query = query.filter_by(difficulty=difficulty)
    if q_type:
        query = query.filter_by(question_type=q_type)

    total     = query.count()
    questions = query.offset((page - 1) * per_page).limit(per_page).all()

    return jsonify({
        "document_id":    doc_id,
        "document_title": doc.title,
        "total":          total,
        "page":           page,
        "per_page":       per_page,
        "questions":      [q.to_dict(include_answer=True) for q in questions],
    })


@admin_bp.route("/documents/<doc_id>/chunks", methods=["GET"])
def list_chunks(doc_id: str):
    SourceDocument.query.get_or_404(doc_id)
    chunks = ContentChunk.query.filter_by(document_id=doc_id)\
                               .order_by(ContentChunk.chunk_index).all()
    return jsonify({"document_id": doc_id, "chunks": [c.to_dict() for c in chunks]})


@admin_bp.route("/documents/<doc_id>", methods=["DELETE"])
def delete_document(doc_id: str):
    doc = SourceDocument.query.get_or_404(doc_id)
    db.session.delete(doc)
    db.session.commit()
    return jsonify({"deleted": doc_id})


@admin_bp.route("/sessions", methods=["GET"])
def list_sessions():
    status   = request.args.get("status")
    doc_id   = request.args.get("document_id")
    page     = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)

    query = QuizSession.query
    if status:
        query = query.filter_by(status=status)
    if doc_id:
        query = query.filter_by(document_id=doc_id)

    total    = query.count()
    sessions = query.order_by(QuizSession.created_at.desc())\
                    .offset((page - 1) * per_page).limit(per_page).all()
    return jsonify({"total": total, "sessions": [s.to_dict() for s in sessions]})


@admin_bp.route("/stats", methods=["GET"])
def stats():
    from sqlalchemy import func

    total_docs      = SourceDocument.query.count()
    ready_docs      = SourceDocument.query.filter_by(status="ready").count()
    total_chunks    = ContentChunk.query.count()
    total_questions = QuizQuestion.query.count()
    total_sessions  = QuizSession.query.count()
    active_sessions = QuizSession.query.filter_by(status="active").count()

    avg_accuracy = db.session.query(
        func.avg(
            func.cast(QuizQuestion.times_correct, db.Float) /
            func.nullif(QuizQuestion.times_shown, 0)
        )
    ).scalar()

    type_counts = {}
    for q_type in ("mcq", "true_false", "fill_in_the_blank"):
        type_counts[q_type] = QuizQuestion.query.filter_by(question_type=q_type).count()

    diff_counts = {}
    for d in range(1, 6):
        diff_counts[str(d)] = QuizQuestion.query.filter_by(difficulty=d).count()

    return jsonify({
        "documents":         {"total": total_docs, "ready": ready_docs},
        "chunks":            total_chunks,
        "questions": {
            "total":         total_questions,
            "by_type":       type_counts,
            "by_difficulty": diff_counts,
        },
        "sessions":          {"total": total_sessions, "active": active_sessions},
        "avg_accuracy_pct":  round(float(avg_accuracy or 0) * 100, 1),
    })


@admin_bp.route("/documents/<doc_id>/check-duplicates", methods=["POST"])
def check_duplicates(doc_id: str):
    SourceDocument.query.get_or_404(doc_id)
    data    = request.get_json(silent=True) or {}
    use_llm = bool(data.get("use_llm", True))

    questions = QuizQuestion.query.filter_by(document_id=doc_id)\
                                  .order_by(QuizQuestion.created_at).all()

    if len(questions) < 2:
        return jsonify({"message": "Not enough questions to compare.", "duplicates": []})

    duplicate_pairs = []
    seen_texts = []

    for q in questions:
        is_dup, reason = is_duplicate(q.question_text, seen_texts, use_llm=use_llm)
        if is_dup:
            duplicate_pairs.append({
                "question_id":   q.id,
                "question_text": q.question_text,
                "reason":        reason,
            })
        else:
            seen_texts.append(q.question_text)

    return jsonify({
        "document_id":      doc_id,
        "total_questions":  len(questions),
        "duplicates_found": len(duplicate_pairs),
        "duplicates":       duplicate_pairs,
        "recommendation":   (
            f"Consider deleting {len(duplicate_pairs)} duplicate questions."
            if duplicate_pairs else "No duplicates detected."
        ),
    })


@admin_bp.route("/questions/<question_id>", methods=["DELETE"])
def delete_question(question_id: str):
    q = QuizQuestion.query.get_or_404(question_id)
    db.session.delete(q)
    db.session.commit()
    return jsonify({"deleted": question_id})