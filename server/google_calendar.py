"""Push household tasks with due dates into *this user's* Google Calendar."""
from datetime import timedelta
from googleapiclient.errors import HttpError

from db import session, Task, TaskSync
from google_auth import service

CAL_SUMMARY = "Kanban Tasks"

_PRIORITY_COLOR = {
    "urgent": "11",  # red
    "high": "5",
    "medium": "9",
    "low": "8",
}


def _ensure_calendar(svc):
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


def _get_sync(s, task_id, user_id):
    row = (s.query(TaskSync)
           .filter_by(task_id=task_id, user_id=user_id)
           .first())
    if not row:
        row = TaskSync(task_id=task_id, user_id=user_id)
        s.add(row); s.flush()
    return row


def sync(user_id, household_id):
    svc = service("calendar", "v3", user_id)
    if not svc:
        return {"ok": False, "error": "not_connected"}

    cal_id = _ensure_calendar(svc)
    s = session()
    created = updated = removed = 0
    try:
        tasks = s.query(Task).filter_by(household_id=household_id).all()
        for t in tasks:
            should_have_event = t.due_at is not None and t.status != "done"
            sync_row = _get_sync(s, t.id, user_id)
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
                    if sync_row.google_event_id:
                        svc.events().update(
                            calendarId=cal_id, eventId=sync_row.google_event_id, body=body
                        ).execute()
                        updated += 1
                    else:
                        ev = svc.events().insert(calendarId=cal_id, body=body).execute()
                        sync_row.google_event_id = ev["id"]
                        created += 1
                except HttpError as e:
                    if e.resp.status == 404 and sync_row.google_event_id:
                        sync_row.google_event_id = None
                    else:
                        print(f"[calendar] error on '{t.title}': {e}")
            else:
                if sync_row.google_event_id:
                    try:
                        svc.events().delete(
                            calendarId=cal_id, eventId=sync_row.google_event_id
                        ).execute()
                        removed += 1
                    except HttpError as e:
                        if e.resp.status != 404:
                            print(f"[calendar] delete failed: {e}")
                    sync_row.google_event_id = None
        s.commit()
    finally:
        s.close()
    return {"ok": True, "created": created, "updated": updated, "removed": removed}


def remove_remote(task, user_id):
    s = session()
    try:
        row = (s.query(TaskSync)
               .filter_by(task_id=task.id, user_id=user_id)
               .first())
        if not row or not row.google_event_id:
            return
        svc = service("calendar", "v3", user_id)
        if not svc:
            return
        try:
            cal_id = _ensure_calendar(svc)
            svc.events().delete(calendarId=cal_id, eventId=row.google_event_id).execute()
        except HttpError as e:
            if e.resp.status != 404:
                print(f"[calendar] delete failed: {e}")
    finally:
        s.close()
