"""Flask app: serves the Kanban frontend + REST API + Google OAuth.

Multi-user: every request is scoped to the signed-in user and the household
they currently have active. Tasks belong to a household; sync (Tasks /
Calendar / Gmail) runs against the *signed-in user's* Google account.
"""
import os
import time
import secrets
from datetime import datetime
from functools import wraps
from pathlib import Path
from flask import (
    Flask, request, jsonify, redirect, session as flask_session,
    send_from_directory, abort,
)
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

from db import (
    init_db, session, with_retry,
    User, Household, Membership, Invite, gen_invite_token,
    Task, EmailSuggestion,
)
import google_auth
import google_tasks
import google_calendar
import gmail_scan

REPO_ROOT = Path(__file__).resolve().parent.parent

app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(32))
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("OAUTH_REDIRECT_URI", "").startswith("https://"),
)
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


# ── Auth helpers ────────────────────────────────────────────────────────────
def _uid():
    """Current user id from the session, or None."""
    return flask_session.get("user_id")


def _active_household_id(s, user_id):
    """Return the active household id for this user. Sets the session value
    on first call (defaults to their oldest household membership)."""
    hid = flask_session.get("active_household_id")
    if hid:
        mem = (s.query(Membership)
               .filter_by(user_id=user_id, household_id=hid).first())
        if mem:
            return hid
        flask_session.pop("active_household_id", None)  # stale, fall through

    mem = (s.query(Membership)
           .filter_by(user_id=user_id)
           .order_by(Membership.joined_at.asc())
           .first())
    if mem:
        flask_session["active_household_id"] = mem.household_id
        return mem.household_id
    return None


def require_user(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not _uid():
            return jsonify({"error": "not_signed_in"}), 401
        return fn(*a, **kw)
    return wrapper


def require_household(fn):
    """Resolve user_id + active household_id and pass them to the view."""
    @wraps(fn)
    def wrapper(*a, **kw):
        uid = _uid()
        if not uid:
            return jsonify({"error": "not_signed_in"}), 401
        s = session()
        try:
            hid = _active_household_id(s, uid)
        finally:
            s.close()
        if not hid:
            return jsonify({"error": "no_household"}), 400
        return fn(uid, hid, *a, **kw)
    return wrapper


# ── Auth endpoints ──────────────────────────────────────────────────────────
@app.get("/api/me")
def me():
    def _go():
        uid = _uid()
        user = google_auth.current_user(uid) if uid else None
        s = session()
        try:
            households = []
            active = None
            if user:
                memberships = (s.query(Membership, Household)
                               .join(Household, Membership.household_id == Household.id)
                               .filter(Membership.user_id == uid)
                               .order_by(Membership.joined_at.asc())
                               .all())
                households = [
                    {"id": h.id, "name": h.name, "role": m.role,
                     "memberCount": s.query(Membership).filter_by(household_id=h.id).count()}
                    for m, h in memberships
                ]
                active = _active_household_id(s, uid)
            # Has the current user connected Google credentials (i.e. can sync)?
            from db import Credentials
            has_creds = bool(uid and s.query(Credentials).filter_by(user_id=uid).first())
            return {
                "signedIn": user is not None,
                "connected": has_creds,
                "user": user,
                "households": households,
                "activeHouseholdId": active,
            }
        finally:
            s.close()
    return jsonify(with_retry(_go))


@app.get("/api/auth/login")
def auth_login():
    url, state, code_verifier = google_auth.authorization_url()
    flask_session["oauth_state"] = state
    flask_session["oauth_code_verifier"] = code_verifier
    # If the user landed via an invite link, remember it so we can claim
    # the invite right after the Google round-trip.
    invite = request.args.get("invite")
    if invite:
        flask_session["pending_invite"] = invite
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
        user = google_auth.exchange_code(code, state, code_verifier=code_verifier)
        flask_session["user_id"] = user["id"]
    except Exception as e:
        app.logger.exception("OAuth callback failed")
        return _auth_error_page(f"{type(e).__name__}: {e}")

    # If they came in via an invite link, accept it now
    invite_token = flask_session.pop("pending_invite", None)
    if invite_token:
        try:
            _accept_invite(invite_token, user["id"])
        except Exception as e:
            app.logger.exception("invite acceptance failed")

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
@require_user
def auth_disconnect():
    """Removes Google tokens but keeps the user / household intact."""
    google_auth.disconnect(_uid())
    return jsonify({"ok": True})


@app.post("/api/auth/signout")
def auth_signout():
    """Clears the Flask session entirely (signs out)."""
    flask_session.clear()
    return jsonify({"ok": True})


# ── Tasks CRUD (scoped to active household) ────────────────────────────────
def _gen_id():
    return "k_" + str(int(time.time() * 1000)) + "_" + secrets.token_hex(3)


def _parse_due(s):
    if not s:
        return None
    try:
        if len(s) == 10:
            return datetime.fromisoformat(s + "T09:00:00")
        return datetime.fromisoformat(s.replace("Z", ""))
    except ValueError:
        return None


@app.get("/api/tasks")
@require_household
def list_tasks(uid, hid):
    def _go():
        s = session()
        try:
            rows = s.query(Task).filter_by(household_id=hid).all()
            return [r.to_dict() for r in rows]
        finally:
            s.close()
    return jsonify(with_retry(_go))


@app.post("/api/tasks")
@require_household
def create_task(uid, hid):
    data = request.get_json(force=True)
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    s = session()
    try:
        t = Task(
            id=_gen_id(),
            household_id=hid,
            created_by_user_id=uid,
            title=title,
            description=data.get("description", ""),
            priority=data.get("priority", "medium"),
            status=data.get("status", "backlog"),
            due_at=_parse_due(data.get("dueAt")),
        )
        if "subtasks" in data:
            t.set_subtasks(data.get("subtasks") or [])
        if "materials" in data:
            t.set_materials(data.get("materials") or [])
        if t.status == "done":
            t.completed_at = datetime.utcnow()
        s.add(t)
        s.commit()
        return jsonify(t.to_dict())
    finally:
        s.close()


@app.patch("/api/tasks/<task_id>")
@require_household
def update_task(uid, hid, task_id):
    data = request.get_json(force=True)
    s = session()
    try:
        t = s.query(Task).filter_by(id=task_id, household_id=hid).first()
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
        if "subtasks" in data:
            t.set_subtasks(data["subtasks"] or [])
        if "materials" in data:
            t.set_materials(data["materials"] or [])
        s.commit()
        return jsonify(t.to_dict())
    finally:
        s.close()


@app.delete("/api/tasks/<task_id>")
@require_household
def delete_task(uid, hid, task_id):
    s = session()
    try:
        t = s.query(Task).filter_by(id=task_id, household_id=hid).first()
        if not t:
            return jsonify({"error": "not found"}), 404
        try: google_tasks.remove_remote(t, uid)
        except Exception as e: print(f"[delete] tasks cleanup: {e}")
        try: google_calendar.remove_remote(t, uid)
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
@require_household
def sync_all(uid, hid):
    if not google_auth.load_credentials(uid):
        return jsonify({"ok": False, "error": "not_connected"}), 401
    return jsonify({
        "tasks":    _safe_run("tasks",    lambda: google_tasks.sync(uid, hid)),
        "calendar": _safe_run("calendar", lambda: google_calendar.sync(uid, hid)),
        "gmail":    _safe_run("gmail",    lambda: gmail_scan.scan(uid, hid)),
    })


# ── Email suggestions ───────────────────────────────────────────────────────
@app.get("/api/suggestions")
@require_user
def list_suggestions():
    return jsonify(gmail_scan.list_suggestions(_uid()))


@app.post("/api/suggestions/<int:sug_id>/accept")
@require_household
def accept_suggestion(uid, hid, sug_id):
    t = gmail_scan.accept_suggestion(sug_id, uid, hid)
    if not t:
        return jsonify({"error": "suggestion not found or already handled"}), 404
    return jsonify(t)


@app.post("/api/suggestions/<int:sug_id>/dismiss")
@require_user
def dismiss_suggestion(sug_id):
    gmail_scan.dismiss_suggestion(sug_id, _uid())
    return jsonify({"ok": True})


# ── Households / sharing ────────────────────────────────────────────────────
@app.post("/api/households")
@require_user
def create_household():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()[:80] or "Household"
    s = session()
    try:
        h = Household(name=name, created_by_user_id=_uid())
        s.add(h); s.flush()
        s.add(Membership(household_id=h.id, user_id=_uid(), role="owner"))
        s.commit()
        flask_session["active_household_id"] = h.id
        return jsonify({"id": h.id, "name": h.name})
    finally:
        s.close()


@app.post("/api/households/<int:hid>/activate")
@require_user
def activate_household(hid):
    s = session()
    try:
        mem = (s.query(Membership)
               .filter_by(user_id=_uid(), household_id=hid).first())
        if not mem:
            return jsonify({"error": "not_a_member"}), 403
        flask_session["active_household_id"] = hid
        return jsonify({"ok": True, "activeHouseholdId": hid})
    finally:
        s.close()


@app.get("/api/households/<int:hid>/members")
@require_user
def list_members(hid):
    s = session()
    try:
        mem = (s.query(Membership)
               .filter_by(user_id=_uid(), household_id=hid).first())
        if not mem:
            return jsonify({"error": "not_a_member"}), 403
        rows = (s.query(Membership, User)
                .join(User, Membership.user_id == User.id)
                .filter(Membership.household_id == hid)
                .order_by(Membership.joined_at.asc())
                .all())
        return jsonify([
            {"userId": u.id, "email": u.email, "name": u.name or u.email,
             "pictureUrl": u.picture_url, "role": m.role,
             "joinedAt": m.joined_at.isoformat() if m.joined_at else None}
            for m, u in rows
        ])
    finally:
        s.close()


@app.delete("/api/households/<int:hid>/members/<int:user_id>")
@require_user
def remove_member(hid, user_id):
    s = session()
    try:
        my_mem = (s.query(Membership)
                  .filter_by(user_id=_uid(), household_id=hid).first())
        if not my_mem:
            return jsonify({"error": "not_a_member"}), 403
        # Only owner can remove others. Members can remove themselves.
        if user_id != _uid() and my_mem.role != "owner":
            return jsonify({"error": "owner_only"}), 403
        target = (s.query(Membership)
                  .filter_by(user_id=user_id, household_id=hid).first())
        if not target:
            return jsonify({"error": "not found"}), 404
        # Don't allow removing the last owner
        if target.role == "owner":
            other_owners = (s.query(Membership)
                            .filter_by(household_id=hid, role="owner")
                            .filter(Membership.user_id != user_id)
                            .count())
            if other_owners == 0:
                return jsonify({"error": "last_owner"}), 400
        s.delete(target)
        s.commit()
        # If they removed themselves, clear active household
        if user_id == _uid():
            flask_session.pop("active_household_id", None)
        return jsonify({"ok": True})
    finally:
        s.close()


@app.post("/api/households/<int:hid>/invites")
@require_user
def create_invite(hid):
    s = session()
    try:
        mem = (s.query(Membership)
               .filter_by(user_id=_uid(), household_id=hid).first())
        if not mem:
            return jsonify({"error": "not_a_member"}), 403
        token = gen_invite_token()
        inv = Invite(token=token, household_id=hid,
                     created_by_user_id=_uid(), role="member")
        s.add(inv); s.commit()
        # Build the shareable link from the configured public URL
        base = os.environ.get("PUBLIC_URL")
        if not base:
            # Best-effort fallback: derive from the request
            base = request.host_url.rstrip("/")
        link = f"{base}/kanban.html?invite={token}"
        return jsonify({"token": token, "link": link})
    finally:
        s.close()


@app.get("/api/invites/<token>")
def get_invite(token):
    """Preview an invite (works even when signed out — shows household name)."""
    s = session()
    try:
        inv = s.get(Invite, token)
        if not inv or inv.accepted_at:
            return jsonify({"error": "not found or already used"}), 404
        h = s.get(Household, inv.household_id)
        u = s.get(User, inv.created_by_user_id)
        return jsonify({
            "householdName": h.name if h else "",
            "invitedBy": (u.name or u.email) if u else "",
            "role": inv.role,
        })
    finally:
        s.close()


def _accept_invite(token, user_id):
    s = session()
    try:
        inv = s.get(Invite, token)
        if not inv or inv.accepted_at:
            return False
        # If already a member, just activate the household
        existing = (s.query(Membership)
                    .filter_by(user_id=user_id, household_id=inv.household_id)
                    .first())
        if not existing:
            s.add(Membership(household_id=inv.household_id, user_id=user_id, role=inv.role))
        inv.accepted_at = datetime.utcnow()
        inv.accepted_by_user_id = user_id
        s.commit()
        flask_session["active_household_id"] = inv.household_id
        return True
    finally:
        s.close()


@app.post("/api/invites/<token>/accept")
@require_user
def accept_invite(token):
    ok = _accept_invite(token, _uid())
    if not ok:
        return jsonify({"error": "invite invalid or already used"}), 400
    return jsonify({"ok": True})


# ── Healthcheck ─────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
