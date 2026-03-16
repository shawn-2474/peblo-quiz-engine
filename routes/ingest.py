"""
Ingest routes:
  POST /api/ingest                     -- upload PDF, async processing
  GET  /api/ingest/<id>/status         -- poll status
  GET  /api/documents                  -- list all documents
  POST /api/generate-quiz              -- Fix 4: re-generate questions for a ready doc
"""

import os
import threading
import json
from flask import Blueprint, request, jsonify, current_app
from werkzeug.utils import secure_filename

from database import db, SourceDocument, ContentChunk, QuizQuestion
from utils.pdf_processor import extract_pdf, chunk_text
from utils.llm import generate_questions_for_chunk
from utils.dedup import filter_duplicates_and_validate

ingest_bp = Blueprint("ingest", __name__)

ALLOWED_EXTENSIONS = {"pdf"}


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# -- POST /api/ingest ----------------------------------------------------------

@ingest_bp.route("/ingest", methods=["POST"])
def ingest():
    """
    Upload a PDF. Returns immediately; processing runs in background thread.

    Form fields:
      file                 -- PDF (required)
      questions_per_chunk  -- int, default 3
      target_difficulty    -- int 1-5, default 3
      grade                -- int, optional (e.g. 1, 3, 4)
      subject              -- str, optional (e.g. "Math", "Science")
    """
    if "file" not in request.files:
        return jsonify({"error": "No file part in request"}), 400

    f = request.files["file"]
    if f.filename == "" or not allowed_file(f.filename):
        return jsonify({"error": "Invalid or missing PDF file"}), 400

    questions_per_chunk = int(request.form.get("questions_per_chunk", 3))
    target_difficulty   = int(request.form.get("target_difficulty", 3))
    questions_per_chunk = max(1, min(questions_per_chunk, 10))
    target_difficulty   = max(1, min(target_difficulty, 5))

    # Optional hint metadata (used if LLM inference fails)
    hint_grade   = request.form.get("grade",   type=int)
    hint_subject = request.form.get("subject", type=str)

    safe_name  = secure_filename(f.filename)
    upload_dir = current_app.config["UPLOAD_FOLDER"]
    save_path  = os.path.join(upload_dir, safe_name)
    f.save(save_path)

    doc = SourceDocument(filename=safe_name, file_path=save_path, status="processing")
    db.session.add(doc)
    db.session.commit()

    app = current_app._get_current_object()
    thread = threading.Thread(
        target=_process_document,
        args=(app, doc.id, save_path, questions_per_chunk, target_difficulty, hint_grade, hint_subject),
        daemon=True,
    )
    thread.start()

    return jsonify({
        "document_id": doc.id,
        "status": "processing",
        "message": "Document accepted. Poll /api/ingest/{id}/status for updates.",
    }), 202


# -- GET /api/ingest/<id>/status -----------------------------------------------

@ingest_bp.route("/ingest/<doc_id>/status", methods=["GET"])
def ingest_status(doc_id: str):
    doc = SourceDocument.query.get_or_404(doc_id)
    return jsonify(doc.to_dict())


# -- GET /api/documents --------------------------------------------------------

@ingest_bp.route("/documents", methods=["GET"])
def list_documents():
    docs = SourceDocument.query.order_by(SourceDocument.created_at.desc()).all()
    return jsonify([d.to_dict() for d in docs])


# -- Fix 4: POST /api/generate-quiz --------------------------------------------

@ingest_bp.route("/generate-quiz", methods=["POST"])
def generate_quiz():
    """
    Standalone endpoint to (re-)generate quiz questions for an already-ingested document.
    Useful for regenerating with different difficulty or question count without re-uploading.

    Body (JSON):
      document_id          string   (required)
      questions_per_chunk  int      (optional, default 3)
      target_difficulty    int 1-5  (optional, default 3)
      replace_existing     bool     (optional, default false — set true to delete old questions first)
    """
    data = request.get_json(silent=True) or {}
    doc_id = data.get("document_id")
    if not doc_id:
        return jsonify({"error": "document_id is required"}), 400

    doc = SourceDocument.query.get_or_404(doc_id)
    if doc.status != "ready":
        return jsonify({"error": f"Document not ready (status: {doc.status}). Ingest it first."}), 409

    chunks = ContentChunk.query.filter_by(document_id=doc_id).order_by(ContentChunk.chunk_index).all()
    if not chunks:
        return jsonify({"error": "No content chunks found. Re-ingest the document."}), 409

    questions_per_chunk = int(data.get("questions_per_chunk", 3))
    target_difficulty   = int(data.get("target_difficulty", 3))
    questions_per_chunk = max(1, min(questions_per_chunk, 10))
    target_difficulty   = max(1, min(target_difficulty, 5))
    replace_existing    = bool(data.get("replace_existing", False))

    if replace_existing:
        QuizQuestion.query.filter_by(document_id=doc_id).delete()
        db.session.commit()

    app = current_app._get_current_object()
    thread = threading.Thread(
        target=_generate_questions_only,
        args=(app, doc_id, chunks, questions_per_chunk, target_difficulty),
        daemon=True,
    )
    thread.start()

    return jsonify({
        "document_id":        doc_id,
        "chunks_to_process":  len([c for c in chunks if len(c.text.split()) >= 50]),
        "questions_per_chunk":questions_per_chunk,
        "target_difficulty":  target_difficulty,
        "replace_existing":   replace_existing,
        "message": "Question generation started. Poll /api/ingest/{id}/status to track.",
    }), 202


# -- Background workers --------------------------------------------------------

def _process_document(app, doc_id, file_path, questions_per_chunk,
                       target_difficulty, hint_grade, hint_subject):
    """Extract PDF -> infer metadata -> chunk -> generate questions."""
    with app.app_context():
        doc = SourceDocument.query.get(doc_id)
        if not doc:
            return
        try:
            # 1. Extract
            result = extract_pdf(file_path)
            doc.page_count = result["page_count"]
            doc.title      = result.get("title") or doc.filename
            db.session.commit()

            if not result["pages"]:
                doc.status    = "error"
                doc.error_msg = "No text could be extracted from the PDF."
                db.session.commit()
                return

            # 2. Infer grade/subject/topic from first ~1500 chars using LLM
            metadata_hint = _infer_document_metadata(
                result["full_text"][:1500],
                hint_grade=hint_grade,
                hint_subject=hint_subject,
            )

            # 3. Chunk
            chunks_data = chunk_text(result["pages"])
            chunk_objs  = []
            for c in chunks_data:
                chunk_obj = ContentChunk(
                    document_id = doc.id,
                    chunk_index = c["chunk_index"],
                    text        = c["text"],
                    page_start  = c["page_start"],
                    page_end    = c["page_end"],
                    token_count = c["token_count"],
                    grade       = hint_grade   or metadata_hint.get("grade"),
                    subject     = hint_subject or metadata_hint.get("subject"),
                    topic       = metadata_hint.get("topic"),
                )
                db.session.add(chunk_obj)
                chunk_objs.append(chunk_obj)
            db.session.commit()

            # 4. Generate questions
            _generate_questions_only(app, doc.id, chunk_objs, questions_per_chunk,
                                     target_difficulty, already_in_context=True)

            doc.status = "ready"
            db.session.commit()

        except Exception as e:
            doc.status    = "error"
            doc.error_msg = str(e)
            db.session.commit()
            print(f"[ingest] document {doc_id} failed: {e}")


def _generate_questions_only(app, doc_id, chunk_objs, questions_per_chunk,
                              target_difficulty, already_in_context=False):
    """Generate, deduplicate, validate, and persist questions for a list of chunks."""
    ctx = app.app_context() if not already_in_context else _nullctx()
    with ctx:
        # Build a running list of all question texts already in the DB for this doc
        existing_texts = [
            q.question_text
            for q in QuizQuestion.query.filter_by(document_id=doc_id).all()
        ]

        total_accepted = 0
        total_rejected = 0

        for chunk_obj in chunk_objs:
            if len(chunk_obj.text.split()) < 10:
                continue
            try:
                raw_questions = generate_questions_for_chunk(
                    chunk_text=chunk_obj.text,
                    n_questions=questions_per_chunk,
                    target_difficulty=target_difficulty,
                )

                # Filter out duplicates and low-quality questions
                accepted, rejected = filter_duplicates_and_validate(
                    new_questions=raw_questions,
                    existing_question_texts=existing_texts,
                    run_quality_check=True,
                )

                if rejected:
                    print(f"[dedup] chunk {chunk_obj.chunk_index}: "
                          f"{len(rejected)} rejected — "
                          + "; ".join(r['rejection_reason'] for r in rejected))

                for q in accepted:
                    if not q.get("question_text") or not q.get("correct_answer"):
                        continue
                    question = QuizQuestion(
                        document_id    = doc_id,
                        chunk_id       = chunk_obj.id,
                        question_text  = q["question_text"],
                        question_type  = q["question_type"],
                        options        = q.get("options"),
                        correct_answer = q["correct_answer"],
                        explanation    = q.get("explanation", ""),
                        difficulty     = q["difficulty"],
                        topic_tags     = q.get("topic_tags", []),
                    )
                    db.session.add(question)
                    existing_texts.append(q["question_text"])  # update running list

                db.session.commit()
                total_accepted += len(accepted)
                total_rejected += len(rejected)

            except Exception as e:
                print(f"[generate] chunk {chunk_obj.chunk_index} error: {e}")
                continue

        print(f"[generate] doc {doc_id}: {total_accepted} accepted, "
              f"{total_rejected} rejected")


def _infer_document_metadata(text_excerpt: str, hint_grade=None, hint_subject=None) -> dict:
    """
    Ask Gemini to infer grade, subject, and topic from the first chunk of text.
    Falls back to empty values if the call fails.
    """
    try:
        from utils.llm import _call_gemini
        prompt = f"""Analyse this educational text excerpt and return JSON with:
{{
  "grade": <integer grade level or null>,
  "subject": "<subject name or null>",
  "topic": "<specific topic or null>"
}}

Examples of subjects: Math, Science, English, History, Geography
Examples of topics: Shapes, Plants and Animals, Grammar, Fractions

TEXT:
\"\"\"{text_excerpt}\"\"\"

Respond with JSON only."""

        raw = _call_gemini(
            prompt,
            system="You are an educational metadata extractor. Respond with JSON only.",
            max_tokens=128,
        )
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)
        if hint_grade:
            result["grade"] = hint_grade
        if hint_subject:
            result["subject"] = hint_subject
        return result
    except Exception as e:
        print(f"[metadata] inference failed: {e}")
        return {"grade": hint_grade, "subject": hint_subject, "topic": None}


class _nullctx:
    """No-op context manager for when we're already inside an app context."""
    def __enter__(self): return self
    def __exit__(self, *a): pass
