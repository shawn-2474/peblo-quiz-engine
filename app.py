"""
PDF Quiz Generator - Main Flask Application
"""

import os
from flask import Flask
from flask_cors import CORS
from database import db, init_db
from routes.ingest import ingest_bp
from routes.quiz import quiz_bp
from routes.admin import admin_bp

def create_app():
    app = Flask(__name__)
    CORS(app)

    # ── Configuration ──────────────────────────────────────────────
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
        "DATABASE_URL",
        "postgresql://quizuser:quizpass@localhost:5432/quizdb"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["UPLOAD_FOLDER"] = "/tmp/uploads"
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB

    try:
     os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    except FileExistsError:
     pass

    # ── Database ───────────────────────────────────────────────────
    db.init_app(app)
    with app.app_context():
        init_db()

    # ── Blueprints ─────────────────────────────────────────────────
    app.register_blueprint(ingest_bp,  url_prefix="/api")
    app.register_blueprint(quiz_bp,    url_prefix="/api")
    app.register_blueprint(admin_bp,   url_prefix="/api/admin")

    @app.route("/health")
    def health():
        return {"status": "ok", "service": "pdf-quiz-api"}

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True, host="0.0.0.0", port=5000)
