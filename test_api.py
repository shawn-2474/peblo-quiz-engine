#!/usr/bin/env python3
"""
PDF Quiz API — end-to-end test / usage demo.

Usage:
    python test_api.py                        # runs against localhost:5000
    python test_api.py https://your-server    # runs against a remote host
"""

import sys
import time
import json
import requests

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:5000"
PDF_PATH = "sample.pdf"   # put any PDF here, or change the path

s = requests.Session()


def pp(label, resp):
    print(f"\n{'='*60}")
    print(f"  {label}  [{resp.status_code}]")
    print(f"{'='*60}")
    try:
        print(json.dumps(resp.json(), indent=2))
    except Exception:
        print(resp.text[:500])


# ── 1. Health check ────────────────────────────────────────────────────────────
pp("GET /health", s.get(f"{BASE}/health"))

# ── 2. Ingest a PDF ────────────────────────────────────────────────────────────
try:
    with open(PDF_PATH, "rb") as f:
        resp = s.post(
            f"{BASE}/api/ingest",
            files={"file": (PDF_PATH, f, "application/pdf")},
            data={"questions_per_chunk": 3, "target_difficulty": 3},
        )
except FileNotFoundError:
    print(f"\n⚠  {PDF_PATH} not found. Create a sample PDF and re-run.")
    sys.exit(1)

pp("POST /api/ingest", resp)
doc_id = resp.json()["document_id"]

# ── 3. Poll until ready ────────────────────────────────────────────────────────
print("\n⏳  Waiting for processing…")
for _ in range(60):
    r = s.get(f"{BASE}/api/ingest/{doc_id}/status")
    status = r.json()["status"]
    print(f"    status: {status}")
    if status == "ready":
        pp("GET /api/ingest/<id>/status — READY", r)
        break
    if status == "error":
        pp("ERROR", r)
        sys.exit(1)
    time.sleep(5)

# ── 4. Start a quiz session ────────────────────────────────────────────────────
resp = s.post(f"{BASE}/api/quiz/start", json={"document_id": doc_id, "user_id": "demo_user"})
pp("POST /api/quiz/start", resp)
session_id = resp.json()["session_id"]

# ── 5. Answer 5 questions ──────────────────────────────────────────────────────
for i in range(5):
    # Get next question
    resp = s.get(f"{BASE}/api/quiz/{session_id}")
    pp(f"GET /api/quiz/<session> — question {i+1}", resp)

    data = resp.json()
    if "summary" in data:
        print("\n🏁  Quiz completed early (all questions exhausted).")
        break

    q = data["question"]
    print(f"\n  Q: {q['question_text']}")
    if q.get("options"):
        for opt in q["options"]:
            print(f"     {opt}")

    # Dummy answer: always pick the first option (for demo purposes)
    if q["question_type"] == "mcq" and q.get("options"):
        my_answer = q["options"][0]
    elif q["question_type"] == "true_false":
        my_answer = "True"
    else:
        my_answer = "I don't know"

    resp = s.post(
        f"{BASE}/api/quiz/{session_id}/answer",
        json={"question_id": q["id"], "answer": my_answer, "time_taken_ms": 3000},
    )
    pp(f"POST /api/quiz/<session>/answer — answer {i+1}", resp)

# ── 6. Final summary ───────────────────────────────────────────────────────────
pp("GET /api/quiz/<session>/summary", s.get(f"{BASE}/api/quiz/{session_id}/summary"))

# ── 7. Admin stats ─────────────────────────────────────────────────────────────
pp("GET /api/admin/stats", s.get(f"{BASE}/api/admin/stats"))
