"""Database models and session helpers."""
import os
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Text, DateTime, ForeignKey
)
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///kanban.db")
# Render's Postgres URLs sometimes come back as postgres:// — SQLAlchemy needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False))
Base = declarative_base()


class Credentials(Base):
    """Single-user credential store. Always id=1."""
    __tablename__ = "credentials"
    id = Column(Integer, primary_key=True)
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

    def to_dict(self):
        return {
            "id": self.id,
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
        }


class EmailSuggestion(Base):
    __tablename__ = "email_suggestions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    email_id = Column(String, unique=True, nullable=False)
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


class SyncState(Base):
    """Tracks the last Gmail scan to avoid re-processing old mail."""
    __tablename__ = "sync_state"
    id = Column(Integer, primary_key=True)  # always 1
    last_gmail_scan_at = Column(DateTime, nullable=True)
    last_full_sync_at = Column(DateTime, nullable=True)


def init_db():
    Base.metadata.create_all(engine)


def session():
    return SessionLocal()
