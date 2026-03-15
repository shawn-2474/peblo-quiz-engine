"""
Database models and initialization
"""

import uuid
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.dialects.postgresql import JSONB

db = SQLAlchemy()


# -- Helpers -------------------------------------------------------------------

def new_uuid():
    return str(uuid.uuid4())


# -- Models --------------------------------------------------------------------

class SourceDocument(db.Model):
    """Original uploaded PDF."""
    __tablename__ = "source_documents"

    id          = db.Column(db.String(36), primary_key=True, default=new_uuid)
    filename    = db.Column(db.String(512), nullable=False)
    title       = db.Column(db.String(512))
    file_path   = db.Column(db.String(1024))
    page_count  = db.Column(db.Integer)
    status      = db.Column(db.String(32), default="pending")  # pending|processing|ready|error
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    error_msg   = db.Column(db.Text)

    chunks      = db.relationship("ContentChunk", back_populates="document", cascade="all, delete-orphan")
    questions   = db.relationship("QuizQuestion", back_populates="document", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id, "filename": self.filename, "title": self.title,
            "page_count": self.page_count, "status": self.status,
            "created_at": self.created_at.isoformat(),
            "chunk_count": len(self.chunks),
            "question_count": len(self.questions),
        }


class ContentChunk(db.Model):
    """Text chunk extracted from a PDF, with educational metadata."""
    __tablename__ = "content_chunks"

    id          = db.Column(db.String(36), primary_key=True, default=new_uuid)
    document_id = db.Column(db.String(36), db.ForeignKey("source_documents.id"), nullable=False)
    chunk_index = db.Column(db.Integer,    nullable=False)
    text        = db.Column(db.Text,       nullable=False)
    page_start  = db.Column(db.Integer)
    page_end    = db.Column(db.Integer)
    token_count = db.Column(db.Integer)
    # Fix 2: educational metadata inferred by LLM during ingestion
    grade       = db.Column(db.Integer)       # e.g. 1, 3, 4
    subject     = db.Column(db.String(128))   # e.g. "Math", "Science", "English"
    topic       = db.Column(db.String(256))   # e.g. "Shapes", "Plants", "Grammar"
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    document    = db.relationship("SourceDocument", back_populates="chunks")
    questions   = db.relationship("QuizQuestion",   back_populates="chunk")

    @property
    def chunk_id(self):
        """Human-readable chunk ID matching the spec format SRC_001_CH_01."""
        return f"{self.document_id[:8].upper()}_CH_{self.chunk_index:02d}"

    def to_dict(self):
        return {
            "id":           self.id,
            "chunk_id":     self.chunk_id,
            "chunk_index":  self.chunk_index,
            "text_preview": self.text[:200] + ("..." if len(self.text) > 200 else ""),
            "page_start":   self.page_start,
            "page_end":     self.page_end,
            "token_count":  self.token_count,
            "grade":        self.grade,
            "subject":      self.subject,
            "topic":        self.topic,
        }


class QuizQuestion(db.Model):
    """LLM-generated quiz question with adaptive metadata."""
    __tablename__ = "quiz_questions"

    id             = db.Column(db.String(36), primary_key=True, default=new_uuid)
    document_id    = db.Column(db.String(36), db.ForeignKey("source_documents.id"), nullable=False)
    chunk_id       = db.Column(db.String(36), db.ForeignKey("content_chunks.id"))
    question_text  = db.Column(db.Text,       nullable=False)
    # Fix 1: question_type now includes fill_in_the_blank
    question_type  = db.Column(db.String(32), default="mcq")  # mcq | true_false | fill_in_the_blank
    options        = db.Column(JSONB)          # list[str] for mcq / true_false; null for fill_in_the_blank
    correct_answer = db.Column(db.Text,        nullable=False)
    explanation    = db.Column(db.Text)
    difficulty     = db.Column(db.Integer,     default=3)   # 1 (easy) -> 5 (hard)
    topic_tags     = db.Column(JSONB,          default=list)
    times_shown    = db.Column(db.Integer,     default=0)
    times_correct  = db.Column(db.Integer,     default=0)
    created_at     = db.Column(db.DateTime,    default=datetime.utcnow)

    document       = db.relationship("SourceDocument", back_populates="questions")
    chunk          = db.relationship("ContentChunk",   back_populates="questions")

    @property
    def accuracy_rate(self):
        return (self.times_correct / self.times_shown) if self.times_shown > 0 else None

    @property
    def source_chunk_id(self):
        """Fix 5: expose source chunk_id in the spec format."""
        if self.chunk:
            return self.chunk.chunk_id
        return None

    def to_dict(self, include_answer=False):
        d = {
            "id":               self.id,
            "question_text":    self.question_text,
            "question_type":    self.question_type,
            "options":          self.options,
            "difficulty":       self.difficulty,
            "topic_tags":       self.topic_tags,
            "times_shown":      self.times_shown,
            "accuracy_rate":    self.accuracy_rate,
            # Fix 5: always include source traceability
            "source_chunk_id":  self.source_chunk_id,
            "document_id":      self.document_id,
        }
        if include_answer:
            d["correct_answer"] = self.correct_answer
            d["explanation"]    = self.explanation
        return d


class QuizSession(db.Model):
    """A user's quiz attempt with adaptive difficulty tracking."""
    __tablename__ = "quiz_sessions"

    id                 = db.Column(db.String(36), primary_key=True, default=new_uuid)
    document_id        = db.Column(db.String(36), db.ForeignKey("source_documents.id"), nullable=False)
    user_id            = db.Column(db.String(256), default="anonymous")
    current_difficulty = db.Column(db.Integer, default=3)
    questions_asked    = db.Column(db.Integer, default=0)
    questions_correct  = db.Column(db.Integer, default=0)
    status             = db.Column(db.String(32), default="active")  # active | completed
    created_at         = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at         = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    answers            = db.relationship("SessionAnswer", back_populates="session", cascade="all, delete-orphan")

    @property
    def score_pct(self):
        return round(self.questions_correct / self.questions_asked * 100, 1) if self.questions_asked else 0

    def to_dict(self):
        return {
            "id": self.id, "document_id": self.document_id, "user_id": self.user_id,
            "current_difficulty": self.current_difficulty,
            "questions_asked":    self.questions_asked,
            "questions_correct":  self.questions_correct,
            "score_pct":          self.score_pct,
            "status":             self.status,
            "created_at":         self.created_at.isoformat(),
        }


class SessionAnswer(db.Model):
    """Individual answer within a quiz session."""
    __tablename__ = "session_answers"

    id                 = db.Column(db.String(36), primary_key=True, default=new_uuid)
    session_id         = db.Column(db.String(36), db.ForeignKey("quiz_sessions.id"), nullable=False)
    question_id        = db.Column(db.String(36), db.ForeignKey("quiz_questions.id"), nullable=False)
    user_answer        = db.Column(db.Text)
    is_correct         = db.Column(db.Boolean)
    time_taken_ms      = db.Column(db.Integer)
    difficulty_at_time = db.Column(db.Integer)
    answered_at        = db.Column(db.DateTime, default=datetime.utcnow)

    session            = db.relationship("QuizSession",  back_populates="answers")
    question           = db.relationship("QuizQuestion")

    def to_dict(self):
        return {
            "id":                self.id,
            "question_id":       self.question_id,
            "user_answer":       self.user_answer,
            "is_correct":        self.is_correct,
            "time_taken_ms":     self.time_taken_ms,
            "difficulty_at_time":self.difficulty_at_time,
            "answered_at":       self.answered_at.isoformat(),
        }


def init_db():
    db.create_all()
