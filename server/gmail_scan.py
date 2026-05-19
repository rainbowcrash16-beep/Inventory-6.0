"""Scan Gmail: auto-create tasks from emails labeled 'kanban', suggest others."""
import re
import time
from datetime import datetime, timedelta
from googleapiclient.errors import HttpError

from db import session, Task, EmailSuggestion, SyncState
from google_auth import service

AUTOLABEL = "kanban"
SUGGEST_QUERY = "is:unread newer_than:7d -category:promotions -category:social"
ACTION_VERBS = (
    "please", "can you", "could you", "would you", "need", "needs",
    "todo", "to-do", "to do", "reminder", "deadline", "due", "follow up",
    "follow-up", "review", "approve", "send", "draft", "respond", "reply",
    "schedule", "book", "buy", "pick up", "call", "submit", "complete",
)
DEADLINE_RE = re.compile(
    r"\b(by|before|due|deadline)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|today|"
    r"next week|this week|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|\d{1,2}[/-]\d{1,2})",
    re.IGNORECASE,
)


def _gen_id():
    return "k_" + str(int(time.time() * 1000)) + "_" + str(int(time.time_ns()))[-5:]


def _find_label_id(svc, name):
    labels = svc.users().labels().list(userId="me").execute().get("labels", [])
    for lab in labels:
        if lab["name"].lower() == name.lower():
            return lab["id"]
    return None


def _header(headers, key):
    for h in headers:
        if h["name"].lower() == key.lower():
            return h["value"]
    return ""


def _looks_like_task(subject, snippet):
    text = (subject + " " + snippet).lower()
    if any(v in text for v in ACTION_VERBS):
        return True
    if "?" in subject:
        return True
    if DEADLINE_RE.search(text):
        return True
    return False


def _suggested_title(subject):
    # Trim "Re:" / "Fwd:" prefixes
    s = re.sub(r"^(re|fwd|fw):\s*", "", subject, flags=re.IGNORECASE).strip()
    return s[:120] if s else "(no subject)"


def scan():
    svc = service("gmail", "v1")
    if not svc:
        return {"ok": False, "error": "not_connected"}

    s = session()
    state = s.get(SyncState, 1)
    if not state:
        state = SyncState(id=1)
        s.add(state)
        s.commit()

    auto_created = suggested = skipped = 0

    try:
        # ── Auto-create from labeled emails ────────────────────────────────
        label_id = _find_label_id(svc, AUTOLABEL)
        if label_id:
            resp = svc.users().messages().list(
                userId="me", labelIds=[label_id], maxResults=50
            ).execute()
            for ref in resp.get("messages", []):
                msg = svc.users().messages().get(
                    userId="me", id=ref["id"], format="metadata",
                    metadataHeaders=["Subject", "From"],
                ).execute()
                email_id = msg["id"]
                # Skip if already converted
                existing = s.query(Task).filter_by(source_email_id=email_id).first()
                if existing:
                    skipped += 1
                    continue
                headers = msg.get("payload", {}).get("headers", [])
                subject = _header(headers, "Subject") or "(no subject)"
                sender = _header(headers, "From") or ""
                snippet = msg.get("snippet", "")
                t = Task(
                    id=_gen_id(),
                    title=_suggested_title(subject),
                    description=f"From: {sender}\n\n{snippet}",
                    priority="medium",
                    status="todo",
                    source_email_id=email_id,
                )
                s.add(t)
                auto_created += 1

        # ── Suggest from unlabeled, recent unread ──────────────────────────
        resp = svc.users().messages().list(
            userId="me", q=SUGGEST_QUERY, maxResults=30
        ).execute()
        for ref in resp.get("messages", []):
            email_id = ref["id"]
            # Skip if labeled (handled above) — quick check by looking up labels
            existing_sug = s.query(EmailSuggestion).filter_by(email_id=email_id).first()
            if existing_sug:
                continue
            existing_task = s.query(Task).filter_by(source_email_id=email_id).first()
            if existing_task:
                continue

            msg = svc.users().messages().get(
                userId="me", id=email_id, format="metadata",
                metadataHeaders=["Subject", "From"],
            ).execute()
            if label_id and label_id in (msg.get("labelIds") or []):
                continue  # already auto-created
            headers = msg.get("payload", {}).get("headers", [])
            subject = _header(headers, "Subject") or ""
            sender = _header(headers, "From") or ""
            snippet = msg.get("snippet", "")
            if not _looks_like_task(subject, snippet):
                continue
            sug = EmailSuggestion(
                email_id=email_id,
                subject=subject[:200],
                sender=sender[:200],
                snippet=snippet[:500],
                suggested_title=_suggested_title(subject),
            )
            s.add(sug)
            suggested += 1

        state.last_gmail_scan_at = datetime.utcnow()
        s.commit()
    except HttpError as e:
        s.rollback()
        return {"ok": False, "error": str(e)}
    finally:
        s.close()

    return {"ok": True, "autoCreated": auto_created, "suggested": suggested, "skipped": skipped}


def accept_suggestion(sug_id):
    s = session()
    try:
        sug = s.get(EmailSuggestion, sug_id)
        if not sug or sug.accepted_task_id or sug.dismissed_at:
            return None
        t = Task(
            id=_gen_id(),
            title=sug.suggested_title or "(no subject)",
            description=f"From: {sug.sender}\n\n{sug.snippet}",
            priority="medium",
            status="todo",
            source_email_id=sug.email_id,
        )
        s.add(t)
        sug.accepted_task_id = t.id
        s.commit()
        return t.to_dict()
    finally:
        s.close()


def dismiss_suggestion(sug_id):
    s = session()
    try:
        sug = s.get(EmailSuggestion, sug_id)
        if sug and not sug.dismissed_at:
            sug.dismissed_at = datetime.utcnow()
            s.commit()
        return True
    finally:
        s.close()


def list_suggestions():
    s = session()
    try:
        rows = (
            s.query(EmailSuggestion)
            .filter(EmailSuggestion.dismissed_at.is_(None))
            .filter(EmailSuggestion.accepted_task_id.is_(None))
            .order_by(EmailSuggestion.created_at.desc())
            .limit(50)
            .all()
        )
        return [r.to_dict() for r in rows]
    finally:
        s.close()
