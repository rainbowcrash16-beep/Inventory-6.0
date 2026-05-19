"""Push Kanban tasks into a 'Kanban' list in Google Tasks."""
from datetime import datetime
from googleapiclient.errors import HttpError

from db import session, Task
from google_auth import service

LIST_NAME = "Kanban"


def _ensure_list(svc):
    """Return tasklist id for our 'Kanban' list, creating it if missing."""
    lists = svc.tasklists().list(maxResults=100).execute().get("items", [])
    for tl in lists:
        if tl.get("title") == LIST_NAME:
            return tl["id"]
    created = svc.tasklists().insert(body={"title": LIST_NAME}).execute()
    return created["id"]


def sync():
    """Push local tasks to Google Tasks. Returns summary dict."""
    svc = service("tasks", "v1")
    if not svc:
        return {"ok": False, "error": "not_connected"}

    tasklist_id = _ensure_list(svc)
    s = session()
    pushed = updated = completed = 0
    try:
        tasks = s.query(Task).all()
        for t in tasks:
            body = {
                "title": t.title,
                "notes": t.description or "",
                "status": "completed" if t.status == "done" else "needsAction",
            }
            if t.due_at:
                # Google Tasks only honors the date portion
                body["due"] = t.due_at.strftime("%Y-%m-%dT00:00:00.000Z")
            if t.status == "done" and t.completed_at:
                body["completed"] = t.completed_at.isoformat() + "Z"

            try:
                if t.google_task_id:
                    svc.tasks().update(
                        tasklist=tasklist_id, task=t.google_task_id, body=body
                    ).execute()
                    updated += 1
                    if t.status == "done":
                        completed += 1
                else:
                    created = svc.tasks().insert(
                        tasklist=tasklist_id, body=body
                    ).execute()
                    t.google_task_id = created["id"]
                    pushed += 1
            except HttpError as e:
                if e.resp.status == 404 and t.google_task_id:
                    # remote was deleted — clear and recreate next run
                    t.google_task_id = None
                else:
                    print(f"[tasks] error on '{t.title}': {e}")
        s.commit()
    finally:
        s.close()

    return {"ok": True, "pushed": pushed, "updated": updated, "completed": completed}


def remove_remote(task):
    """Delete a single task from Google Tasks (used when local task is deleted)."""
    if not task.google_task_id:
        return
    svc = service("tasks", "v1")
    if not svc:
        return
    try:
        tasklist_id = _ensure_list(svc)
        svc.tasks().delete(tasklist=tasklist_id, task=task.google_task_id).execute()
    except HttpError as e:
        print(f"[tasks] delete failed: {e}")
