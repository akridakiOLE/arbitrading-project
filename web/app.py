"""
web/app.py - Flask backend for arbitrading bot dashboard (v4 Phase 3b).

Single-user auth via password (from env var ARBITRADING_WEB_PASSWORD).
Session-based login. Runs behind nginx reverse proxy.

Routes:
  GET  /                      login page (if not logged in) or redirect to /dashboard
  POST /login                 authenticate
  GET  /logout                end session
  GET  /dashboard             main UI page
  GET  /api/status            bot status (JSON)
  GET  /api/trades            recent trades (JSON)
  GET  /api/state             recent state snapshots (JSON)
  POST /api/start             start bot with config
  POST /api/stop              stop bot
  POST /api/resset            Promote 3 trigger
  POST /api/config            save config
"""

import os
import sqlite3
import logging
from functools import wraps
from pathlib import Path

from flask import Flask, request, jsonify, session, render_template, redirect, url_for

from web.bot_manager import get_manager

logger = logging.getLogger(__name__)


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    # Session secret from env (persistent across restarts)
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
    # Single-user password (bcrypt/plain - keep simple for now)
    password = os.environ.get("ARBITRADING_WEB_PASSWORD", "changeme")

    def login_required(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not session.get("logged_in"):
                if request.path.startswith("/api/"):
                    return jsonify({"error": "unauthorized"}), 401
                return redirect(url_for("login_page"))
            return f(*args, **kwargs)
        return wrapper

    # --- Auth ---

    @app.route("/")
    def index():
        if session.get("logged_in"):
            return redirect(url_for("dashboard"))
        return redirect(url_for("login_page"))

    @app.route("/login", methods=["GET"])
    def login_page():
        return render_template("login.html", error=None)

    @app.route("/login", methods=["POST"])
    def login_post():
        submitted = request.form.get("password", "")
        if submitted == password:
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        return render_template("login.html", error="Λάθος password"), 401

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login_page"))

    # --- Dashboard UI ---

    @app.route("/dashboard")
    @login_required
    def dashboard():
        return render_template("dashboard.html")

    # --- JSON APIs ---

    @app.route("/api/status")
    @login_required
    def api_status():
        return jsonify(get_manager().status())

    @app.route("/api/start", methods=["POST"])
    @login_required
    def api_start():
        cfg = request.get_json() or {}
        return jsonify(get_manager().start(cfg))

    @app.route("/api/stop", methods=["POST"])
    @login_required
    def api_stop():
        return jsonify(get_manager().stop())

    @app.route("/api/resset", methods=["POST"])
    @login_required
    def api_resset():
        return jsonify(get_manager().resset_invest())

    @app.route("/api/config")
    @login_required
    def api_config():
        return jsonify(get_manager().load_config())

    @app.route("/api/trades")
    @login_required
    def api_trades():
        mode = request.args.get("mode", "paper")
        db = "live_trades.db" if mode == "live" else "paper_trades.db"
        if not Path(db).exists():
            return jsonify([])
        try:
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            table = "live_trades" if mode == "live" else "paper_trades"
            rows = conn.execute(
                f"SELECT * FROM {table} ORDER BY id DESC LIMIT 50"
            ).fetchall()
            conn.close()
            return jsonify([dict(r) for r in rows])
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/state")
    @login_required
    def api_state():
        mode = request.args.get("mode", "paper")
        db = "live_state.db" if mode == "live" else "paper_state.db"
        if not Path(db).exists():
            return jsonify([])
        try:
            conn = sqlite3.connect(db)
            rows = conn.execute(
                "SELECT ts_iso, event, state FROM state_snapshots "
                "ORDER BY id DESC LIMIT 20"
            ).fetchall()
            conn.close()
            return jsonify([{"ts_iso": r[0], "event": r[1], "state": r[2]} for r in rows])
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True})

    return app


app = create_app()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    app.run(host="0.0.0.0", port=5001, debug=False)
