"""Flask app: serves the Kanban frontend + REST API + Google OAuth."""
import os
import time
import secrets
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify, redirect, session as flask_session, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

from db import init_db, session, Task, EmailSuggestion
import google_auth
import google_tasks
import google_calendar
import gmail_scan

REPO_ROOT = Path(__file__).resolve().parent.parent

app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(32))
CORS(app, supports_credentials=True)

init_db()


# ── Static frontend ─────────────────────────────────────────────────────────
@app.route("/")
def root():
    return send_from_directory(REPO_ROOT, "index.html")


@app.route("/<path:filename>")
def static_file(filename):
    if filename.startswith("api/"):
        return jsonify({"error": "not found"}), 404
    return send_from_directory(REPO_ROOT, filename)


# ── Auth ────────────────────────────────────────────────────────────────────
@app.get("/api/me")
def me():
    user = google_auth.current_user()
    return jsonify({"connected": user is not None, "user": user})


@app.get("/api/auth/login")
def auth_login():
    url, state, code_verifier = google_auth.authorization_url()
    flask_session["oauth_state"] = state
    flask_session["oauth_code_verifier"] = code_verifier
    return redirect(url)


@app.get("/api/auth/callback")
def auth_callback():
    state = flask_session.get("oauth_state") or request.args.get("state")
    code_verifier = flask_session.pop("oauth_code_verifier", None)
    code = request.args.get("code")
    err = request.args.get("error")
    if err:
        return _auth_error_page(f"Google returned an error: {err}")
    if not code:
        return _auth_error_page("Missing authorization code in callback.")
    try:
        google_auth.exchange_code(code, state, code_verifier=code_verifier)
    except Exception as e:
        app.logger.exception("OAuth callback failed")
        return _auth_error_page(f"{type(e).__name__}: {e}")
    return redirect("/kanban.html?connected=1")


def _auth_error_page(message):
    safe = (message or "Unknown error").replace("<", "&lt;").replace(">", "&gt;")
    html = f"""<!doctype html><meta charset="utf-8">
<title>Sign-in failed</title>
<body style="background:#0A0A0F;color:#E8E8F0;font-family:monospace;padding:40px;line-height:1.5">
<h1 style="color:#FF4D6D">Sign-in failed</h1>
<pre style="background:#12121A;border:1px solid #1E1E2E;padding:14px;border-radius:8px;white-space:pre-wrap;word-break:break-word">{safe}</pre>
<p>Check the server logs for the full traceback, then <a href="/kanban.html" style="color:#00D4AA">go back</a>.</p>
</body>"""
    return html, 500


@app.post("/api/auth/disconnect")
def auth_disconnect():
    google_auth.disconnect()
    return jsonify({"ok": True})


# ── Tasks CRUD ──────────────────────────────────────────────────────────────
def _gen_id():
    return "k_" + str(int(time.time() * 1000)) + "_" + secrets.token_hex(3)


def _parse_due(s):
    if not s:
        return None
    try:
        # accept full ISO or just YYYY-MM-DD
        if len(s) == 10:
            return datetime.fromisoformat(s + "T09:00:00")
        return datetime.fromisoformat(s.replace("Z", ""))
    except ValueError:
        return None


@app.get("/api/tasks")
def list_tasks():
    s = session()
    try:
        rows = s.query(Task).all()
        return jsonify([r.to_dict() for r in rows])
    finally:
        s.close()


@app.post("/api/tasks")
def create_task():
    data = request.get_json(force=True)
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    s = session()
    try:
        t = Task(
            id=_gen_id(),
            title=title,
            description=data.get("description", ""),
            priority=data.get("priority", "medium"),
            status=data.get("status", "backlog"),
            due_at=_parse_due(data.get("dueAt")),
        )
        if t.status == "done":
            t.completed_at = datetime.utcnow()
        s.add(t)
        s.commit()
        return jsonify(t.to_dict())
    finally:
        s.close()


@app.patch("/api/tasks/<task_id>")
def update_task(task_id):
    data = request.get_json(force=True)
    s = session()
    try:
        t = s.get(Task, task_id)
        if not t:
            return jsonify({"error": "not found"}), 404
        if "title" in data: t.title = data["title"]
        if "description" in data: t.description = data["description"]
        if "priority" in data: t.priority = data["priority"]
        if "status" in data:
            new_status = data["status"]
            was_done = t.status == "done"
            t.status = new_status
            if new_status == "done" and not was_done:
                t.completed_at = datetime.utcnow()
            elif new_status != "done":
                t.completed_at = None
        if "dueAt" in data:
            t.due_at = _parse_due(data["dueAt"])
        s.commit()
        return jsonify(t.to_dict())
    finally:
        s.close()


@app.delete("/api/tasks/<task_id>")
def delete_task(task_id):
    s = session()
    try:
        t = s.get(Task, task_id)
        if not t:
            return jsonify({"error": "not found"}), 404
        # Best-effort remote cleanup before local delete
        try: google_tasks.remove_remote(t)
        except Exception as e: print(f"[delete] tasks cleanup: {e}")
        try: google_calendar.remove_remote(t)
        except Exception as e: print(f"[delete] calendar cleanup: {e}")
        s.delete(t)
        s.commit()
        return jsonify({"ok": True})
    finally:
        s.close()


# ── Sync ────────────────────────────────────────────────────────────────────
def _safe_run(name, fn):
    try:
        return fn()
    except Exception as e:
        app.logger.exception("sync step '%s' failed", name)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/api/sync")
def sync_all():
    user = google_auth.current_user()
    if not user:
        return jsonify({"ok": False, "error": "not_connected"}), 401
    return jsonify({
        "tasks":    _safe_run("tasks",    google_tasks.sync),
        "calendar": _safe_run("calendar", google_calendar.sync),
        "gmail":    _safe_run("gmail",    gmail_scan.scan),
    })


# ── Email suggestions ───────────────────────────────────────────────────────
@app.get("/api/suggestions")
def list_suggestions():
    return jsonify(gmail_scan.list_suggestions())


@app.post("/api/suggestions/<int:sug_id>/accept")
def accept_suggestion(sug_id):
    t = gmail_scan.accept_suggestion(sug_id)
    if not t:
        return jsonify({"error": "suggestion not found or already handled"}), 404
    return jsonify(t)


@app.post("/api/suggestions/<int:sug_id>/dismiss")
def dismiss_suggestion(sug_id):
    gmail_scan.dismiss_suggestion(sug_id)
    return jsonify({"ok": True})


# ── Healthcheck ─────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
