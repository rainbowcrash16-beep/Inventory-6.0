"""Push Kanban tasks with due dates into Google Calendar as 30-minute events."""
from datetime import timedelta
from googleapiclient.errors import HttpError

from db import session, Task
from google_auth import service

CAL_SUMMARY = "Kanban Tasks"


def _ensure_calendar(svc):
    """Return calendar id for our 'Kanban Tasks' calendar, creating it if missing."""
    page_token = None
    while True:
        resp = svc.calendarList().list(pageToken=page_token).execute()
        for cal in resp.get("items", []):
            if cal.get("summary") == CAL_SUMMARY:
                return cal["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    created = svc.calendars().insert(body={"summary": CAL_SUMMARY, "timeZone": "UTC"}).execute()
    return created["id"]


_PRIORITY_COLOR = {
    "urgent": "11",  # red ("Tomato")
    "high": "5",     # yellow ("Banana")
    "medium": "9",   # blue ("Blueberry")
    "low": "8",      # gray ("Graphite")
}


def sync():
    svc = service("calendar", "v3")
    if not svc:
        return {"ok": False, "error": "not_connected"}

    cal_id = _ensure_calendar(svc)
    s = session()
    created = updated = removed = 0
    try:
        tasks = s.query(Task).all()
        for t in tasks:
            should_have_event = (
                t.due_at is not None and t.status != "done"
            )
            if should_have_event:
                start = t.due_at
                end = start + timedelta(minutes=30)
                body = {
                    "summary": f"[{t.priority.upper()}] {t.title}",
                    "description": t.description or "",
                    "start": {"dateTime": start.isoformat() + "Z", "timeZone": "UTC"},
                    "end":   {"dateTime": end.isoformat()   + "Z", "timeZone": "UTC"},
                    "colorId": _PRIORITY_COLOR.get(t.priority, "9"),
                }
                try:
                    if t.google_event_id:
                        svc.events().update(
                            calendarId=cal_id, eventId=t.google_event_id, body=body
                        ).execute()
                        updated += 1
                    else:
                        ev = svc.events().insert(calendarId=cal_id, body=body).execute()
                        t.google_event_id = ev["id"]
                        created += 1
                except HttpError as e:
                    if e.resp.status == 404 and t.google_event_id:
                        t.google_event_id = None
                    else:
                        print(f"[calendar] error on '{t.title}': {e}")
            else:
                # task should NOT have an event — remove if one exists
                if t.google_event_id:
                    try:
                        svc.events().delete(
                            calendarId=cal_id, eventId=t.google_event_id
                        ).execute()
                        removed += 1
                    except HttpError as e:
                        if e.resp.status != 404:
                            print(f"[calendar] delete failed: {e}")
                    t.google_event_id = None
        s.commit()
    finally:
        s.close()
    return {"ok": True, "created": created, "updated": updated, "removed": removed}


def remove_remote(task):
    if not task.google_event_id:
        return
    svc = service("calendar", "v3")
    if not svc:
        return
    try:
        cal_id = _ensure_calendar(svc)
        svc.events().delete(calendarId=cal_id, eventId=task.google_event_id).execute()
    except HttpError as e:
        if e.resp.status != 404:
            print(f"[calendar] delete failed: {e}")
