"""Push household tasks into a 'Kanban' list in *this user's* Google Tasks."""
from googleapiclient.errors import HttpError

from db import session, Task, TaskSync
from google_auth import service

LIST_NAME = "Kanban"


def _ensure_list(svc):
    lists = svc.tasklists().list(maxResults=100).execute().get("items", [])
    for tl in lists:
        if tl.get("title") == LIST_NAME:
            return tl["id"]
    created = svc.tasklists().insert(body={"title": LIST_NAME}).execute()
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
    svc = service("tasks", "v1", user_id)
    if not svc:
        return {"ok": False, "error": "not_connected"}

    tasklist_id = _ensure_list(svc)
    s = session()
    pushed = updated = completed = 0
    try:
        tasks = s.query(Task).filter_by(household_id=household_id).all()
        for t in tasks:
            body = {
                "title": t.title,
                "notes": t.description or "",
                "status": "completed" if t.status == "done" else "needsAction",
            }
            if t.due_at:
                body["due"] = t.due_at.strftime("%Y-%m-%dT00:00:00.000Z")
            if t.status == "done" and t.completed_at:
                body["completed"] = t.completed_at.isoformat() + "Z"

            sync_row = _get_sync(s, t.id, user_id)
            try:
                if sync_row.google_task_id:
                    svc.tasks().update(
                        tasklist=tasklist_id, task=sync_row.google_task_id, body=body
                    ).execute()
                    updated += 1
                    if t.status == "done":
                        completed += 1
                else:
                    created = svc.tasks().insert(
                        tasklist=tasklist_id, body=body
                    ).execute()
                    sync_row.google_task_id = created["id"]
                    pushed += 1
            except HttpError as e:
                if e.resp.status == 404 and sync_row.google_task_id:
                    sync_row.google_task_id = None
                else:
                    print(f"[tasks] error on '{t.title}': {e}")
        s.commit()
    finally:
        s.close()

    return {"ok": True, "pushed": pushed, "updated": updated, "completed": completed}


def remove_remote(task, user_id):
    """Delete this user's Google Tasks copy of a task that was just deleted."""
    s = session()
    try:
        row = (s.query(TaskSync)
               .filter_by(task_id=task.id, user_id=user_id)
               .first())
        if not row or not row.google_task_id:
            return
        svc = service("tasks", "v1", user_id)
        if not svc:
            return
        try:
            tasklist_id = _ensure_list(svc)
            svc.tasks().delete(tasklist=tasklist_id, task=row.google_task_id).execute()
        except HttpError as e:
            print(f"[tasks] delete failed: {e}")
    finally:
        s.close()
