"""Database models and session helpers."""
import os
import json
import secrets
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Text, DateTime, ForeignKey, inspect, text,
)
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///kanban.db")
# Render's Postgres URLs sometimes come back as postgres:// — SQLAlchemy needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

_is_sqlite = DATABASE_URL.startswith("sqlite")
_is_pg = "postgresql" in DATABASE_URL

# Neon's free tier suspends compute after ~5min idle, which drops live
# Postgres connections. pool_recycle proactively rotates connections under
# that window; TCP keepalives detect a dead socket faster on slow networks.
_connect_args = {}
if _is_sqlite:
    _connect_args = {"check_same_thread": False}
elif _is_pg:
    _connect_args = {
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
        "connect_timeout": 10,
    }

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=280,
    connect_args=_connect_args,
)
SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False))
Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    google_id = Column(String, unique=True, nullable=False, index=True)
    email = Column(String, nullable=False)
    name = Column(String, nullable=True)
    picture_url = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_seen_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name or self.email,
            "pictureUrl": self.picture_url,
        }


class Household(Base):
    __tablename__ = "households"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, default="My Tasks")
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)


class Membership(Base):
    __tablename__ = "memberships"
    id = Column(Integer, primary_key=True, autoincrement=True)
    household_id = Column(Integer, ForeignKey("households.id"), nullable=False, index=True)
    user_id      = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    role = Column(String, nullable=False, default="member")   # 'owner' | 'member'
    joined_at = Column(DateTime, default=datetime.utcnow)


class Invite(Base):
    __tablename__ = "invites"
    token = Column(String, primary_key=True)
    household_id = Column(Integer, ForeignKey("households.id"), nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    role = Column(String, nullable=False, default="member")
    expires_at = Column(DateTime, nullable=True)
    accepted_at = Column(DateTime, nullable=True)
    accepted_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


def gen_invite_token():
    return secrets.token_urlsafe(16)


class Credentials(Base):
    """Google OAuth credentials, one row per user."""
    __tablename__ = "credentials"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=True, index=True)
    google_id = Column(String, nullable=False)
    email = Column(String, nullable=False)
    refresh_token = Column(Text, nullable=True)
    access_token = Column(Text, nullable=True)
    token_expiry = Column(DateTime, nullable=True)
    scopes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Task(Base):
    __tablename__ = "tasks"
    id = Column(String, primary_key=True)
    household_id = Column(Integer, ForeignKey("households.id"), nullable=True, index=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    title = Column(String, nullable=False)
    description = Column(Text, default="")
    priority = Column(String, default="medium")
    status = Column(String, default="backlog")
    due_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    google_task_id = Column(String, nullable=True)
    google_event_id = Column(String, nullable=True)
    source_email_id = Column(String, nullable=True)
    # JSON-encoded list of {"id": str, "text": str, "done": bool}. Stored as
    # Text so the same DDL works on SQLite and Postgres without a typed JSON
    # column (and so the in-place migration in _migrate() is portable).
    subtasks = Column(Text, nullable=True)
    materials = Column(Text, nullable=True)

    def _get_list(self, raw):
        if not raw:
            return []
        try:
            arr = json.loads(raw)
            return arr if isinstance(arr, list) else []
        except (ValueError, TypeError):
            return []

    def _clean_list(self, items, extra=()):
        if items is None:
            return None
        clean = []
        for s in items:
            if not isinstance(s, dict):
                continue
            sid = str(s.get("id") or "").strip() or _gen_sub_id()
            txt = str(s.get("text") or "").strip()
            if not txt:
                continue
            row = {"id": sid, "text": txt[:200], "done": bool(s.get("done"))}
            for k in extra:
                if k == "qty":
                    q = s.get("qty")
                    try:
                        q = int(q) if q not in (None, "", False) else None
                    except (TypeError, ValueError):
                        q = None
                    if q is not None and q < 1: q = 1
                    if q is not None and q > 9999: q = 9999
                    if q is not None: row["qty"] = q
                elif k == "cost":
                    c = s.get("cost")
                    try:
                        c = float(c) if c not in (None, "", False) else None
                    except (TypeError, ValueError):
                        c = None
                    if c is not None and c < 0: c = 0.0
                    if c is not None: row["cost"] = round(c, 2)
                elif k == "done_at":
                    v = s.get("done_at")
                    if v:
                        row["done_at"] = str(v)[:40]
                else:
                    if k in s and s[k] is not None:
                        row[k] = s[k]
            clean.append(row)
        return json.dumps(clean) if clean else None

    def get_subtasks(self):  return self._get_list(self.subtasks)
    def get_materials(self): return self._get_list(self.materials)
    def set_subtasks(self, items):  self.subtasks  = self._clean_list(items)
    def set_materials(self, items): self.materials = self._clean_list(items, extra=("qty", "cost", "done_at"))

    def to_dict(self):
        return {
            "id": self.id,
            "householdId": self.household_id,
            "createdByUserId": self.created_by_user_id,
            "title": self.title,
            "description": self.description or "",
            "priority": self.priority,
            "status": self.status,
            "dueAt": self.due_at.isoformat() if self.due_at else None,
            "createdAt": int(self.created_at.timestamp() * 1000) if self.created_at else None,
            "updatedAt": int(self.updated_at.timestamp() * 1000) if self.updated_at else None,
            "completedAt": int(self.completed_at.timestamp() * 1000) if self.completed_at else None,
            "googleTaskId": self.google_task_id,
            "googleEventId": self.google_event_id,
            "sourceEmailId": self.source_email_id,
            "subtasks": self.get_subtasks(),
            "materials": self.get_materials(),
        }


def _gen_sub_id():
    import secrets
    return "s_" + secrets.token_hex(4)


class EmailSuggestion(Base):
    __tablename__ = "email_suggestions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    email_id = Column(String, nullable=False)
    subject = Column(String, default="")
    sender = Column(String, default="")
    snippet = Column(Text, default="")
    suggested_title = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    dismissed_at = Column(DateTime, nullable=True)
    accepted_task_id = Column(String, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "emailId": self.email_id,
            "subject": self.subject,
            "sender": self.sender,
            "snippet": self.snippet,
            "suggestedTitle": self.suggested_title,
            "createdAt": int(self.created_at.timestamp() * 1000) if self.created_at else None,
        }


class TaskSync(Base):
    """Per-user Google Tasks / Calendar IDs for a task. Each member of a
    household syncs the shared board to their own Google account, so the
    Google IDs must be tracked per user, not on the task itself."""
    __tablename__ = "task_sync"
    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey("tasks.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    google_task_id = Column(String, nullable=True)
    google_event_id = Column(String, nullable=True)


class SyncState(Base):
    """Per-user state — last Gmail scan, last sync timestamp."""
    __tablename__ = "sync_state"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=True, index=True)
    last_gmail_scan_at = Column(DateTime, nullable=True)
    last_full_sync_at = Column(DateTime, nullable=True)


def init_db():
    Base.metadata.create_all(engine)
    _migrate()


def _migrate():
    """Add columns to existing tables, then promote legacy single-user data
    into the multi-user shape (users / households / memberships). Each step
    is idempotent — safe to run repeatedly on startup."""
    insp = inspect(engine)
    if "tasks" not in insp.get_table_names():
        return

    # --- column additions on existing tables ---
    table_alters = {
        "tasks": {
            "subtasks":            "ALTER TABLE tasks ADD COLUMN subtasks TEXT",
            "materials":           "ALTER TABLE tasks ADD COLUMN materials TEXT",
            "household_id":        "ALTER TABLE tasks ADD COLUMN household_id INTEGER",
            "created_by_user_id":  "ALTER TABLE tasks ADD COLUMN created_by_user_id INTEGER",
        },
        "credentials": {
            "user_id":             "ALTER TABLE credentials ADD COLUMN user_id INTEGER",
        },
        "email_suggestions": {
            "user_id":             "ALTER TABLE email_suggestions ADD COLUMN user_id INTEGER",
        },
        "sync_state": {
            "user_id":             "ALTER TABLE sync_state ADD COLUMN user_id INTEGER",
        },
    }
    with engine.begin() as conn:
        for table, additions in table_alters.items():
            if table not in insp.get_table_names():
                continue
            cols = {c["name"] for c in insp.get_columns(table)}
            for col, ddl in additions.items():
                if col in cols:
                    continue
                try:
                    conn.execute(text(ddl))
                    print(f"[db] migrated: {ddl}")
                except Exception as e:
                    print(f"[db] migration step failed ({ddl}): {e}")

        # email_suggestions used to have UNIQUE on email_id from the
        # single-user era. With multi-user that's wrong (two users can
        # legitimately have a suggestion for the same Gmail thread id),
        # and on Postgres it surfaces as IntegrityError gkpj when the
        # scanner re-runs on the same inbox. Drop it on whichever
        # dialect we're on.
        if "email_suggestions" in insp.get_table_names():
            _drop_legacy_email_id_unique(conn)

    _backfill_single_user_data()


def _drop_legacy_email_id_unique(conn):
    """Drop the legacy UNIQUE on email_suggestions.email_id, regardless of
    how it was created (CONSTRAINT vs. standalone INDEX) and what dialect
    we're on. Silent on failure — if nothing matches, the constraint was
    already gone."""
    dialect = engine.dialect.name
    if dialect == "postgresql":
        try:
            # Find every unique constraint on the email_id column and drop it.
            rows = conn.execute(text("""
                SELECT con.conname FROM pg_constraint con
                JOIN pg_class rel ON rel.oid = con.conrelid
                JOIN pg_attribute att ON att.attrelid = rel.oid
                                      AND att.attnum = ANY(con.conkey)
                WHERE rel.relname = 'email_suggestions'
                  AND att.attname = 'email_id'
                  AND con.contype = 'u'
            """)).fetchall()
            for (name,) in rows:
                conn.execute(text(f'ALTER TABLE email_suggestions DROP CONSTRAINT "{name}"'))
                print(f"[db] dropped legacy unique constraint: {name}")
            # Also drop any leftover standalone unique index on email_id alone
            rows = conn.execute(text("""
                SELECT i.relname FROM pg_index x
                JOIN pg_class i ON i.oid = x.indexrelid
                JOIN pg_class t ON t.oid = x.indrelid
                JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(x.indkey)
                WHERE t.relname = 'email_suggestions'
                  AND a.attname = 'email_id'
                  AND x.indisunique = true
                  AND array_length(x.indkey, 1) = 1
            """)).fetchall()
            for (name,) in rows:
                conn.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
                print(f"[db] dropped legacy unique index: {name}")
        except Exception as e:
            print(f"[db] dropping legacy email_id unique failed: {e}")
    elif dialect == "sqlite":
        try:
            needs_rebuild = False
            indexes = conn.execute(
                text("PRAGMA index_list('email_suggestions')")
            ).fetchall()
            for row in indexes:
                idx_name, is_unique = row[1], row[2]
                if not is_unique:
                    continue
                cols = conn.execute(
                    text(f"PRAGMA index_info('{idx_name}')")
                ).fetchall()
                if len(cols) == 1 and cols[0][2] == "email_id":
                    if idx_name.startswith("sqlite_autoindex_"):
                        # Auto-indexes from CREATE TABLE can only be removed
                        # by rebuilding the table without the UNIQUE clause.
                        needs_rebuild = True
                    else:
                        conn.execute(text(f"DROP INDEX {idx_name}"))
                        print(f"[db] dropped legacy unique index: {idx_name}")
            if needs_rebuild:
                cols_info = conn.execute(
                    text("PRAGMA table_info('email_suggestions')")
                ).fetchall()
                col_names = [c[1] for c in cols_info]
                cols_csv = ", ".join(col_names)
                # Use the model to recreate the table shape — keeps us
                # in sync with whatever columns are current.
                EmailSuggestion.__table__.name = "email_suggestions_new"
                try:
                    EmailSuggestion.__table__.create(conn)
                finally:
                    EmailSuggestion.__table__.name = "email_suggestions"
                conn.execute(text(
                    f"INSERT INTO email_suggestions_new ({cols_csv}) "
                    f"SELECT {cols_csv} FROM email_suggestions"
                ))
                conn.execute(text("DROP TABLE email_suggestions"))
                conn.execute(text(
                    "ALTER TABLE email_suggestions_new RENAME TO email_suggestions"
                ))
                print("[db] rebuilt email_suggestions to drop legacy UNIQUE on email_id")
        except Exception as e:
            print(f"[db] dropping legacy email_id unique failed: {e}")


def _backfill_single_user_data():
    """If the database used to be single-user (one credentials row, no
    households), promote that into a User + Household + Membership and
    attach all existing tasks. Runs at most once: the second call is a
    no-op because there will already be a household."""
    s = SessionLocal()
    try:
        # Any household already exists → assume backfill ran before
        existing_household = s.query(Household).first()
        if existing_household:
            return

        # Try to find legacy credentials (id=1 from the single-user era,
        # or any credentials row without a user_id).
        legacy = (
            s.query(Credentials)
            .filter((Credentials.user_id.is_(None)) | (Credentials.id == 1))
            .first()
        )

        user = None
        if legacy and legacy.google_id:
            user = User(
                google_id=legacy.google_id,
                email=legacy.email or "unknown@unknown",
                name=legacy.email or "",
            )
            s.add(user); s.flush()
            legacy.user_id = user.id

        # Create a default household. If we found a user, they're the owner.
        # If not, we still create one so future signups can use it (the
        # first signer-in becomes its owner via the OAuth callback path).
        h = Household(name="My Tasks", created_by_user_id=user.id if user else None)
        s.add(h); s.flush()

        if user:
            s.add(Membership(household_id=h.id, user_id=user.id, role="owner"))

        # Attach all orphan tasks (household_id IS NULL) to this household.
        try:
            s.execute(text(
                "UPDATE tasks SET household_id = :hid WHERE household_id IS NULL"
            ), {"hid": h.id})
            if user:
                s.execute(text(
                    "UPDATE tasks SET created_by_user_id = :uid "
                    "WHERE created_by_user_id IS NULL"
                ), {"uid": user.id})
        except Exception as e:
            print(f"[db] backfill UPDATE tasks failed: {e}")

        # Promote the per-task google_task_id / google_event_id values into
        # task_sync rows owned by the legacy user. Old columns stay on tasks
        # for safety but won't be used by the new sync path.
        if user:
            try:
                s.execute(text("""
                    INSERT INTO task_sync (task_id, user_id, google_task_id, google_event_id)
                    SELECT id, :uid, google_task_id, google_event_id
                    FROM tasks
                    WHERE (google_task_id IS NOT NULL OR google_event_id IS NOT NULL)
                      AND id NOT IN (SELECT task_id FROM task_sync WHERE user_id = :uid)
                """), {"uid": user.id})
            except Exception as e:
                print(f"[db] backfill task_sync failed: {e}")

            # Adopt legacy email_suggestions rows for this user so the
            # scanner's dedupe check picks them up and we don't try to
            # re-insert duplicate email_ids.
            try:
                s.execute(text(
                    "UPDATE email_suggestions SET user_id = :uid "
                    "WHERE user_id IS NULL"
                ), {"uid": user.id})
            except Exception as e:
                print(f"[db] backfill email_suggestions.user_id failed: {e}")

        s.commit()
        print(f"[db] backfilled: user_id={user.id if user else None} household_id={h.id}")
    finally:
        s.close()


def session():
    return SessionLocal()


def with_retry(fn, retries=1):
    """Run fn(), and if it fails with a transient DB error, dispose the pool
    and retry once. Covers the case where Neon's compute woke up mid-query."""
    from sqlalchemy.exc import OperationalError, InterfaceError, DBAPIError
    for attempt in range(retries + 1):
        try:
            return fn()
        except (OperationalError, InterfaceError, DBAPIError) as e:
            if attempt >= retries:
                raise
            print(f"[db] transient error, retrying: {e}")
            try:
                SessionLocal.remove()
                engine.dispose()
            except Exception:
                pass
