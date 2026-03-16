# PDF Quiz Generator — Backend API

A production-ready Flask backend that ingests PDFs, extracts content,
generates quiz questions with Groq AI (LLaMA 3.3), stores everything in PostgreSQL,
and serves an adaptive quiz API.

---

## Architecture

```
PDF Upload → pdfplumber extraction → Groq AI metadata inference (grade/subject/topic)
          → text chunking → Groq generates questions (MCQ, True/False, Fill-in-the-blank)
          → duplicate detection → quality validation → PostgreSQL
          → Flask API serves adaptive quiz sessions
          → Rolling-window difficulty engine adjusts per answer
```

---

## Quick Start (Docker)

```bash
cd pdf_quiz_app

# Copy env template and add your free Groq API key
# Get one free at: https://console.groq.com (no credit card needed)
cp .env.example .env
# Edit .env → set GROQ_API_KEY=gsk_...

docker compose up --build
curl http://localhost:5000/health
```

## Quick Start (Local Python)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
createdb quizdb

# Windows PowerShell:
$env:GROQ_API_KEY="gsk_..."
$env:DATABASE_URL="postgresql://localhost/quizdb"

# Mac/Linux:
export GROQ_API_KEY=gsk_...
export DATABASE_URL=postgresql://localhost/quizdb

python app.py
```

---

## API Reference

### Ingest

| Endpoint | Method | Description |
|---|---|---|
| `/api/ingest` | POST | Upload PDF (async) |
| `/api/ingest/{id}/status` | GET | Poll processing status |
| `/api/documents` | GET | List all documents |
| `/api/generate-quiz` | POST | Re-generate questions for a document |

**POST /api/ingest** form fields:
- `file` — PDF (required)
- `questions_per_chunk` — int, default 3
- `target_difficulty` — int 1-5, default 3
- `grade` — int, optional override
- `subject` — string, optional override

**POST /api/generate-quiz** body:
```json
{"document_id": "uuid", "questions_per_chunk": 4, "target_difficulty": 2, "replace_existing": true}
```

---

### Quiz

| Endpoint | Method | Description |
|---|---|---|
| `/api/quiz` | GET | Filter questions by topic/difficulty |
| `/api/quiz/start` | POST | Start adaptive session |
| `/api/quiz/{session_id}` | GET | Get next question |
| `/api/quiz/{session_id}/answer` | POST | Submit answer |
| `/api/quiz/{session_id}/summary` | GET | End session + score report |
| `/api/quiz/{session_id}/history` | GET | Full answer history |

**GET /api/quiz** query params:
```
/api/quiz?topic=shapes&difficulty=easy
/api/quiz?type=fill_in_the_blank&difficulty=medium&limit=5
```
Difficulty accepts: `easy` / `medium` / `hard` or `1`-`5`.

**POST /api/quiz/{session_id}/answer**:
```json
{"question_id": "uuid", "answer": "three", "time_taken_ms": 4200}
```
MCQ: pass `"A"` or `"A. Full option text"` — both accepted.
Fill-in-the-blank: pass the word/phrase directly.

**Answer response:**
```json
{
  "is_correct": true,
  "correct_answer": "three",
  "explanation": "...",
  "new_difficulty": 3,
  "session_score_pct": 80.0,
  "questions_asked": 5
}
```

**Summary performance bands:** `excellent` (>=85%) · `proficient` (>=70%) · `developing` (>=50%) · `needs_review` (<50%)

---

### Admin

| Endpoint | Method | Description |
|---|---|---|
| `/api/admin/stats` | GET | Stats with type + difficulty breakdown |
| `/api/admin/documents/{id}/questions` | GET | All questions (filterable) |
| `/api/admin/documents/{id}/chunks` | GET | All chunks with metadata |
| `/api/admin/documents/{id}` | DELETE | Delete document + all data |
| `/api/admin/documents/{id}/check-duplicates` | POST | Scan for duplicate questions |
| `/api/admin/questions/{id}` | DELETE | Delete a single question |
| `/api/admin/sessions` | GET | List all sessions |

---

## LLM Provider

This project uses **Groq AI** with the `llama-3.3-70b-versatile` model via the free tier.

- Get a free API key at: https://console.groq.com
- No credit card required
- No daily limits on the free tier
- Very fast inference

Groq AI is used for:
- Generating quiz questions from text chunks
- Inferring grade/subject/topic metadata from PDF content
- Grading fill-in-the-blank answers
- Detecting semantically duplicate questions
- Scoring question quality before saving

---

## Duplicate Detection

Two-stage pipeline runs automatically on every generated question:

1. **Fast string similarity** — normalised ratio >= 0.92 = instant reject (no API call)
2. **Groq semantic check** — ratio 0.70-0.92 triggers Groq to verify if they ask the same thing

Post-ingestion scan:
```bash
curl -X POST http://localhost:5000/api/admin/documents/{id}/check-duplicates
```

---

## Question Quality Validation

Each question is scored 0.0-1.0 by Groq before saving:

| Score | Result |
|---|---|
| >= 0.6 | Accepted |
| < 0.6 | Rejected (logged, not saved) |

Checks: unambiguous phrasing, plausible MCQ distractors, fill-in-blank has a meaningful blank, correct answer is genuinely correct.

---

## Adaptive Difficulty Engine

Rolling window over last 5 answers:

| Accuracy | Action |
|---|---|
| >= 80% | Difficulty +1 |
| 50-79% | Hold |
| <= 50% | Difficulty -1 |

Selection order: exact match -> +/-1 -> +/-2 -> any. Prefers least-shown questions.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GROQ_API_KEY` | YES | Free Groq API key from console.groq.com |
| `DATABASE_URL` | YES | PostgreSQL connection string |
| `FLASK_ENV` | no | `development` or `production` |
| `FLASK_DEBUG` | no | `1` for debug mode |

---

## Sample Outputs

See `sample_outputs/` for:
- `extracted_chunks.json` — chunk structure with grade/subject/topic
- `generated_questions.json` — all three question types with source traceability
- `api_responses.json` — complete request/response examples for every endpoint
- `schema.sql` — full PostgreSQL schema with indexes

---

## Running Tests

```powershell
# Windows — put any PDF in the folder as sample.pdf, then:
python test_api.py

# Against a remote server:
python test_api.py https://your-api.example.com
```
