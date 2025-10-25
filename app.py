# test change from Codex
from __future__ import annotations
import io
import json
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Optional
from urllib.parse import quote
from textwrap import shorten

from collections import Counter

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template_string,
    request,
    send_file,
    session,
    url_for,
)
import uuid

import requests
from jinja2 import DictLoader
from markupsafe import escape
from msal import ConfidentialClientApplication
from werkzeug.utils import secure_filename
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    create_engine,
    desc,
    func,
    select,
)
from sqlalchemy.orm import (
    Session,
    declarative_base,
    joinedload,
    relationship,
    scoped_session,
    sessionmaker,
)

GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"
_app_token_cache: dict[str, float | str] = {"token": "", "expires": 0.0}


def _graph_send_mail(token: str, endpoint: str, payload: dict) -> bool:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(endpoint, headers=headers, json=payload, timeout=10)
    except Exception as exc:
        app.logger.warning("Graph sendMail request failed: %s", exc)
        return False
    if resp.status_code in (200, 202):
        return True
    app.logger.warning(
        "Graph sendMail returned %s: %s",
        resp.status_code,
        resp.text[:200],
    )
    return False


def _get_app_graph_token() -> str | None:
    if not (CLIENT_ID and CLIENT_SECRET and AUTHORITY):
        return None
    now = time.time()
    cached_token = _app_token_cache.get("token") or ""
    expires = float(_app_token_cache.get("expires") or 0.0)
    if cached_token and now < expires - 60:
        return str(cached_token)
    try:
        result = msal_app().acquire_token_for_client(scopes=[GRAPH_DEFAULT_SCOPE])
    except Exception as exc:
        app.logger.warning("Unable to acquire app token: %s", exc)
        return None
    token = result.get("access_token")
    if not token:
        error = result.get("error_description") or result.get("error") or "Unknown error"
        app.logger.warning("App token missing from MSAL response: %s", error)
        return None
    expires_in = int(result.get("expires_in") or 0)
    _app_token_cache["token"] = token
    _app_token_cache["expires"] = now + max(0, expires_in)
    return token


def _resolve_sender_address() -> Optional[str]:
    configured = os.getenv("MICROSOFT_MAIL_SENDER")
    if configured:
        return configured.strip()
    if ADMIN_EMAILS:
        return sorted(ADMIN_EMAILS)[0]
    return None


def send_email(to_addrs, subject, html_body):
    """Send an email via Microsoft Graph using delegated or application tokens."""

    if isinstance(to_addrs, str):
        to_addrs = [to_addrs]
    to_addrs = [addr for addr in to_addrs if addr]
    if not to_addrs:
        return False

    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to_addrs],
        }
    }

    token = session.get("access_token")
    if token and _graph_send_mail(token, "https://graph.microsoft.com/v1.0/me/sendMail", payload):
        return True

    sender = _resolve_sender_address()
    app_token = _get_app_graph_token()
    if sender and app_token:
        endpoint = f"https://graph.microsoft.com/v1.0/users/{quote(sender)}/sendMail"
        if _graph_send_mail(app_token, endpoint, payload):
            return True

    app.logger.warning("Failed to send email to %s", ", ".join(to_addrs))
    return False


class OpenAIServiceError(RuntimeError):
    """Raised when an OpenAI API request cannot be fulfilled."""


def _openai_headers() -> dict[str, str]:
    if not OPENAI_API_KEY:
        raise OpenAIServiceError("OPENAI_API_KEY is not configured.")
    return {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }


def _openai_post(path: str, payload: dict, timeout: int = 30) -> dict:
    base = OPENAI_BASE_URL.rstrip("/")
    url = f"{base}/{path.lstrip('/')}"
    try:
        response = requests.post(url, headers=_openai_headers(), json=payload, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise OpenAIServiceError(f"OpenAI request failed: {exc}") from exc
    try:
        data = response.json()
    except ValueError as exc:  # JSONDecodeError inherits from ValueError
        raise OpenAIServiceError("OpenAI response was not valid JSON.") from exc
    return data


def embed_text(text: str) -> list[float]:
    if not text.strip():
        raise OpenAIServiceError("Cannot embed empty text.")
    data = _openai_post(
        "embeddings",
        {
            "model": OPENAI_EMBED_MODEL,
            "input": text,
        },
    )
    try:
        embedding = data["data"][0]["embedding"]
    except (KeyError, IndexError) as exc:
        raise OpenAIServiceError("Embedding missing from OpenAI response.") from exc
    if not isinstance(embedding, list):
        raise OpenAIServiceError("Embedding payload was not a list.")
    try:
        return [float(val) for val in embedding]
    except (TypeError, ValueError) as exc:
        raise OpenAIServiceError("Embedding values were not numeric.") from exc


def generate_answer(question: str, context: str) -> str:
    prompt = (
        "You are an IT help assistant for Seegars. Only answer workplace IT questions (accounts, email, "
        "software, devices, network, security). If out of scope, say you can only answer Seegars IT topics "
        "and suggest creating a ticket."
    )
    payload = {
        "model": OPENAI_CHAT_MODEL,
        "messages": [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": (
                    "Knowledge Base Context:\n" f"{context}\n\n"
                    f"Employee Question: {question.strip()}"
                ),
            },
        ],
        "temperature": 0.2,
        "max_tokens": 500,
    }
    data = _openai_post("chat/completions", payload, timeout=45)
    try:
        answer = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise OpenAIServiceError("Answer missing from OpenAI response.") from exc
    return (answer or "").strip()


IT_ALLOWLIST_TERMS = {
    "network",
    "wifi",
    "vpn",
    "email",
    "outlook",
    "account",
    "login",
    "printer",
    "software",
    "accountmate",
    "remote desktop",
    "password",
    "mfa",
    "windows",
    "mac",
    "laptop",
    "desktop",
    "monitor",
    "teams",
    "sharepoint",
}

IT_BLOCKLIST_TERMS = {
    "recipes",
    "sports",
    "politics",
    "finance",
    "medical diagnosis",
}


def is_it_question(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    if any(term in normalized for term in IT_BLOCKLIST_TERMS):
        return False
    return any(term in normalized for term in IT_ALLOWLIST_TERMS)


def _load_embedding(raw: str | None) -> list[float] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    vector: list[float] = []
    try:
        for value in data:
            vector.append(float(value))
    except (TypeError, ValueError):
        return None
    return vector


def cosine_similarity(vector_a: list[float], vector_b: list[float]) -> float:
    if not vector_a or not vector_b or len(vector_a) != len(vector_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vector_a, vector_b))
    norm_a = math.sqrt(sum(a * a for a in vector_a))
    norm_b = math.sqrt(sum(b * b for b in vector_b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _truncate_body(text: str, limit: int | None = None) -> str:
    if limit is None:
        limit = KB_CONTEXT_BODY_LIMIT
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""
    return shorten(cleaned, width=limit, placeholder="…")


def _kb_unavailable_message() -> str:
    return (
        "I couldn't find a documented solution in the knowledge base. Please create a support "
        "ticket so Seegars IT can help you directly."
    )

# --------------------------------------------------------------------------------------
# Flask app config
# --------------------------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET", "dev-secret")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB overall request cap


def _candidate_path_from_env(value: str) -> Path | None:
    """Return a filesystem path for supported SQLite URI formats."""

    cleaned = value.strip()
    if not cleaned or cleaned == ":memory:":
        return None

    # SQLite supports URIs such as sqlite:///path/to/db.sqlite or sqlite://relative.db
    if cleaned.startswith("sqlite:///"):
        cleaned = cleaned[len("sqlite:///"):]
    elif cleaned.startswith("sqlite://"):
        cleaned = cleaned[len("sqlite://"):]

    if cleaned == ":memory:":
        return None

    if cleaned.startswith("file:") or "://" in cleaned:
        # Treat as a raw SQLite connection string (e.g., file::memory:?cache=shared)
        return None

    if "?" in cleaned:
        cleaned = cleaned.split("?", 1)[0]

    return Path(cleaned)


def _resolve_db_path(
    env_override: str | None = None,
    data_dir_override: str | None = None,
) -> str:
    """Determine the SQLite database location and ensure the directory exists."""

    env_value = env_override if env_override is not None else os.environ.get("TICKETS_DB")
    data_dir = data_dir_override if data_dir_override is not None else os.environ.get("RENDER_DATA_DIR")

    candidate: Path | None = None
    if env_value:
        path_candidate = _candidate_path_from_env(env_value)
        if path_candidate is None:
            return env_value
        candidate = path_candidate.expanduser()
    else:
        base_dir = Path(data_dir) if data_dir else Path(app.instance_path)
        base_dir.mkdir(parents=True, exist_ok=True)
        candidate = (base_dir / "tickets.db").expanduser()

    if not candidate.is_absolute():
        base_dir = Path(data_dir) if data_dir else Path(app.instance_path)
        base_dir.mkdir(parents=True, exist_ok=True)
        candidate = (base_dir / candidate).resolve()

    candidate.parent.mkdir(parents=True, exist_ok=True)
    return str(candidate)


DB_PATH = _resolve_db_path()

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL:
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        connect_args={"sslmode": "require"},
    )
else:
    engine = create_engine(
        f"sqlite:///{DB_PATH}",
        connect_args={"check_same_thread": False},
    )

app.logger.info("DB engine: %s", "Postgres" if DATABASE_URL else f"SQLite @ {DB_PATH}")

SessionLocal = scoped_session(
    sessionmaker(bind=engine, autoflush=False, autocommit=False)
)
Base = declarative_base()


class Ticket(Base):
    __tablename__ = "tickets"

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=False)
    requester_name = Column(String)
    requester_email = Column(String)
    branch = Column(String)
    priority = Column(String)
    category = Column(String)
    assignee = Column(String)
    status = Column(String)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    completed_at = Column(DateTime(timezone=True))
    feedback_token = Column(String)

    comments = relationship(
        "Comment",
        back_populates="ticket",
        lazy="select",
        cascade="all, delete-orphan",
    )
    attachments = relationship(
        "Attachment",
        back_populates="ticket",
        lazy="select",
        cascade="all, delete-orphan",
    )
    feedback_entries = relationship(
        "TicketFeedback",
        back_populates="ticket",
        lazy="select",
        cascade="all, delete-orphan",
    )


class Comment(Base):
    __tablename__ = "comments"

    id = Column(Integer, primary_key=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False)
    author = Column(String)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)

    ticket = relationship("Ticket", back_populates="comments", lazy="select")


class Attachment(Base):
    __tablename__ = "attachments"

    id = Column(Integer, primary_key=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False)
    filename = Column(String, nullable=False)
    content_type = Column(String)
    data = Column(LargeBinary, nullable=False)
    uploaded_at = Column(DateTime(timezone=True), nullable=False)

    ticket = relationship("Ticket", back_populates="attachments", lazy="select")


class TicketFeedback(Base):
    __tablename__ = "ticket_feedback"

    id = Column(Integer, primary_key=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False)
    rating = Column(Integer)
    comments = Column(Text)
    submitted_by = Column(String)
    submitted_at = Column(DateTime(timezone=True), nullable=False)

    ticket = relationship("Ticket", back_populates="feedback_entries", lazy="select")

# Microsoft Entra (Azure AD / M365) app details come from environment variables on Render
CLIENT_ID = os.getenv("MICROSOFT_CLIENT_ID")
TENANT_ID = os.getenv("MICROSOFT_TENANT_ID")
CLIENT_SECRET = os.getenv("MICROSOFT_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")  # e.g., https://seegarsit.onrender.com/auth/callback
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}" if TENANT_ID else None
SCOPE = ["User.Read", "Mail.Send"]


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_CHAT_MODEL = os.getenv("OPENAI_KB_MODEL", "gpt-5.1-mini")
OPENAI_EMBED_MODEL = os.getenv("OPENAI_KB_EMBED_MODEL", "text-embedding-3-small")
try:
    KB_RELEVANCE_THRESHOLD = float(os.getenv("KB_RELEVANCE_THRESHOLD", "0.65"))
except ValueError:
    KB_RELEVANCE_THRESHOLD = 0.65
KB_MAX_CONTEXT_ARTICLES = 5
KB_CONTEXT_BODY_LIMIT = 700


# --------------------------------------------------------------------------------------
# DB helpers
# --------------------------------------------------------------------------------------


def get_session():
    return SessionLocal()


def close_session(exc: BaseException | None = None):  # noqa: ARG001
    SessionLocal.remove()


@app.teardown_appcontext
def _teardown_sqlalchemy(exc: BaseException | None):  # noqa: ARG001
    close_session(exc)


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db

@app.teardown_appcontext
def close_connection(exception: Optional[BaseException]):  # noqa: ARG001
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            requester_name TEXT,
            requester_email TEXT,
            branch TEXT,
            priority TEXT CHECK(priority IN ('Low','Medium','High')) DEFAULT 'Medium',
            category TEXT,
            assignee TEXT,
            status TEXT CHECK(status IN ('Open','In Progress','Waiting','Resolved','Closed')) DEFAULT 'Open',
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            completed_at TIMESTAMP,
            feedback_token TEXT
        );

        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER NOT NULL,
            author TEXT,
            body TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL,
            FOREIGN KEY(ticket_id) REFERENCES tickets(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            content_type TEXT,
            data BLOB NOT NULL,
            uploaded_at TIMESTAMP NOT NULL,
            FOREIGN KEY(ticket_id) REFERENCES tickets(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS ticket_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER NOT NULL,
            rating INTEGER CHECK(rating BETWEEN 1 AND 5),
            comments TEXT,
            submitted_by TEXT,
            submitted_at TIMESTAMP NOT NULL,
            FOREIGN KEY(ticket_id) REFERENCES tickets(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS kb_articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            body TEXT NOT NULL,
            tags TEXT,
            source_ticket_id INTEGER,
            created_at TIMESTAMP NOT NULL,
            helpful_up INTEGER DEFAULT 0,
            helpful_down INTEGER DEFAULT 0,
            FOREIGN KEY(source_ticket_id) REFERENCES tickets(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS kb_embeddings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id INTEGER UNIQUE NOT NULL,
            embedding_json TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL,
            FOREIGN KEY(article_id) REFERENCES kb_articles(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS kb_queries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email TEXT,
            query TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_kb_queries_created ON kb_queries(created_at);

        CREATE INDEX IF NOT EXISTS idx_ticket_feedback_ticket ON ticket_feedback(ticket_id);

        CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
        CREATE INDEX IF NOT EXISTS idx_tickets_priority ON tickets(priority);
        CREATE INDEX IF NOT EXISTS idx_tickets_branch ON tickets(branch);
        CREATE INDEX IF NOT EXISTS idx_comments_ticket ON comments(ticket_id);
        CREATE INDEX IF NOT EXISTS idx_attachments_ticket ON attachments(ticket_id);
        CREATE INDEX IF NOT EXISTS idx_kb_articles_created ON kb_articles(created_at);
        CREATE INDEX IF NOT EXISTS idx_kb_articles_source ON kb_articles(source_ticket_id);
        """
    )
    db.commit()

    # Ensure legacy databases include the completed_at column
    try:
        db.execute("ALTER TABLE tickets ADD COLUMN completed_at TIMESTAMP")
        db.commit()
    except sqlite3.OperationalError:
        # Column already exists
        pass

    try:
        db.execute("ALTER TABLE tickets ADD COLUMN feedback_token TEXT")
        db.commit()
    except sqlite3.OperationalError:
        # Column already exists
        pass

    rows = db.execute(
        "SELECT id FROM tickets WHERE feedback_token IS NULL OR feedback_token = ''"
    ).fetchall()
    if rows:
        for row in rows:
            db.execute(
                "UPDATE tickets SET feedback_token = ? WHERE id = ?",
                (generate_feedback_token(), row["id"]),
            )
        db.commit()

    try:
        Base.metadata.create_all(engine)
    except Exception as e:  # pragma: no cover - safeguard for startup
        app.logger.warning("SQLAlchemy create_all failed: %s", e)

# --------------------------------------------------------------------------------------
# Auth helpers
# --------------------------------------------------------------------------------------

def msal_app() -> ConfidentialClientApplication:
    if not (CLIENT_ID and CLIENT_SECRET and AUTHORITY):
        raise RuntimeError("M365 env vars missing (MICROSOFT_CLIENT_ID/SECRET, MICROSOFT_TENANT_ID).")
    return ConfidentialClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        client_credential=CLIENT_SECRET,
    )


def login_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login", next=request.path))
        return view_func(*args, **kwargs)
    return wrapper

# --------------------------------------------------------------------------------------
# Constants / helpers
# --------------------------------------------------------------------------------------
STATUSES = ["Open", "In Progress", "Waiting", "Resolved", "Closed"]
COMPLETED_STATUSES = {"Resolved", "Closed"}
PRIORITIES = ["Low", "Medium", "High"]
ASSIGNEE_DEFAULT = "Brad Wells"
MAX_ATTACHMENT_TOTAL_BYTES = 10 * 1024 * 1024  # 10 MB per ticket submission
STATUS_BADGES = {
    "Open": {"cls": "badge-chip badge-open", "icon": "bi bi-lightning-charge"},
    "In Progress": {"cls": "badge-chip badge-progress", "icon": "bi bi-arrow-repeat"},
    "Waiting": {"cls": "badge-chip badge-waiting", "icon": "bi bi-hourglass-split"},
    "Resolved": {"cls": "badge-chip badge-complete", "icon": "bi bi-check-circle"},
    "Closed": {"cls": "badge-chip badge-closed", "icon": "bi bi-check2-all"},
}
PRIORITY_BADGES = {
    "High": {"cls": "badge-chip priority-high", "icon": "bi bi-exclamation-octagon"},
    "Medium": {"cls": "badge-chip priority-medium", "icon": "bi bi-activity"},
    "Low": {"cls": "badge-chip priority-low", "icon": "bi bi-arrow-down"},
}
BRANCHES = [
    "Goldsboro","Allison","Augusta","Cary","Columbia","Durham","Fayetteville",
    "Greensboro","Greenville","Jacksonville","Metalcrafters","New Hanover",
    "Newport","Raleigh","Rocky Mount","Spartanburg","Wayne County"
]

CATEGORIES = [
    "Email",
    "Network / Internet",
    "Printer",
    "Software (e.g., AccountMate)",
    "Hardware (e.g., monitor)",
    "User Access (new / existing)",
    "Other",
]

FEEDBACK_RATING_OPTIONS = [
    (5, "Excellent"),
    (4, "Good"),
    (3, "Fair"),
    (2, "Needs improvement"),
    (1, "Poor"),
]

def _load_admin_emails() -> set[str]:
    """Return the set of admin email addresses configured for the app."""

    raw = os.getenv(
        "ADMIN_EMAILS",
        "brad@seegarsfence.com,winston@seegarsfence.com",
    )
    emails = {item.strip().lower() for item in raw.split(",") if item.strip()}
    return emails


ADMIN_EMAILS = _load_admin_emails()

def now_ts():
    return datetime.now(timezone.utc).isoformat()


def generate_feedback_token() -> str:
    return uuid.uuid4().hex


def format_file_size(num_bytes: int) -> str:
    """Convert a byte count to a human-friendly label."""
    if num_bytes <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB"]
    value = float(num_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def current_user_email() -> Optional[str]:
    user = session.get("user") or {}
    email = user.get("email")
    if email:
        return email.lower()
    return None


def is_admin_user() -> bool:
    email = current_user_email()
    return bool(email and email in ADMIN_EMAILS)


def get_ticket_contact(ticket_id: int, db: Optional[sqlite3.Connection] = None):
    if db is None:
        db = get_db()
    row = db.execute(
        """
        SELECT id, title, requester_name, requester_email, feedback_token
        FROM tickets
        WHERE id = ?
        """,
        (ticket_id,),
    ).fetchone()
    return row


def ensure_ticket_feedback_token(ticket_id: int, db: Optional[sqlite3.Connection] = None) -> str:
    if db is None:
        db = get_db()
    row = get_ticket_contact(ticket_id, db=db)
    if not row:
        return ""
    token = row["feedback_token"]
    if token:
        return token
    token = generate_feedback_token()
    db.execute(
        "UPDATE tickets SET feedback_token = ? WHERE id = ?",
        (token, ticket_id),
    )
    db.commit()
    return token


def ticket_detail_link(ticket_id: int) -> str:
    return url_for("ticket_detail", ticket_id=ticket_id, _external=True)


def ticket_feedback_link(ticket_id: int, token: str) -> str:
    return url_for("ticket_feedback", ticket_id=ticket_id, token=token, _external=True)


def send_ticket_notification(
    ticket_row: sqlite3.Row,
    subject: str,
    body_html: str,
):
    email = (ticket_row or {}).get("requester_email") if isinstance(ticket_row, dict) else None
    if ticket_row and not isinstance(ticket_row, dict):
        email = ticket_row["requester_email"]
    if not email:
        return False
    # Normalize to dict for templating convenience
    if not isinstance(ticket_row, dict):
        ticket_row = dict(ticket_row)
    recipient = ticket_row.get("requester_email")
    if not recipient:
        return False
    return send_email(recipient, subject, body_html)


def format_timestamp(value: Optional[str]) -> str:
    if not value:
        return "—"
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            text = str(value)
            # Normalize trailing Z to be ISO compliant
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
        return dt.strftime("%b %d, %Y %I:%M %p UTC")
    except Exception:
        return str(value)

# --------------------------------------------------------------------------------------
# Templates (kept inline for single-file simplicity)
# --------------------------------------------------------------------------------------
BASE_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Seegars IT Tickets</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css" rel="stylesheet">
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&family=Open+Sans:wght@400;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --sg-black: #231F20;
      --sg-green: #008752;
      --sg-green-dark: #006e43;
      --sg-lime: #BCD531;
      --sg-gray: #DEE0D9;
      --sg-offwhite: #F5F6F4;
      --sg-surface: #ffffff;
      --sg-shadow: 0 18px 35px rgba(0, 0, 0, 0.08);
    }

    * { box-sizing: border-box; }

    html, body {
      height: 100%;
      background: radial-gradient(circle at top, rgba(188,213,49,.12), transparent 55%), var(--sg-offwhite);
      color: var(--sg-black);
      font-family: "Outfit", "Open Sans", "Segoe UI", "Helvetica Neue", Arial, sans-serif;
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
    }

    body.has-shell {
      display: flex;
      flex-direction: column;
    }

    a { color: var(--sg-green); text-decoration: none; }
    a:hover { color: var(--sg-green-dark); }

    .app-shell {
      display: flex;
      flex: 1;
      min-height: 100vh;
    }

    .app-header {
      background: linear-gradient(135deg, var(--sg-black), #171819);
      color: #fff;
      padding: 0.85rem 1.5rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      border-bottom: 4px solid var(--sg-green);
      position: sticky;
      top: 0;
      z-index: 1020;
      box-shadow: 0 10px 25px rgba(0, 0, 0, 0.25);
    }

    .brand-mark {
      display: flex;
      align-items: center;
      gap: 0.75rem;
      font-weight: 600;
      letter-spacing: 0.02em;
      font-size: 1.15rem;
    }

    .brand-icon {
      height: 40px;
      width: 40px;
      border-radius: 12px;
      background: radial-gradient(circle at top, rgba(188,213,49,0.45), rgba(0,135,82,0.9));
      display: grid;
      place-items: center;
      font-size: 1.35rem;
    }

    .app-user-meta {
      display: flex;
      align-items: center;
      gap: 1rem;
      font-size: 0.875rem;
    }

    .app-user-meta .btn {
      border-radius: 999px;
      padding-inline: 1.1rem;
    }

    .app-sidebar {
      width: 240px;
      background: rgba(35, 31, 32, 0.92);
      color: rgba(255, 255, 255, 0.82);
      padding: 1.5rem 1.25rem 2.5rem;
      display: flex;
      flex-direction: column;
      gap: 2rem;
      position: sticky;
      top: 72px;
      height: calc(100vh - 72px);
      overflow-y: auto;
    }

    .nav-section-title {
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: rgba(255, 255, 255, 0.52);
      margin-bottom: 0.75rem;
    }

    .nav-pill {
      display: flex;
      align-items: center;
      gap: 0.65rem;
      padding: 0.65rem 0.85rem;
      border-radius: 12px;
      color: inherit;
      transition: background 0.2s ease, transform 0.2s ease;
    }

    .nav-pill:hover {
      background: rgba(188, 213, 49, 0.12);
      transform: translateX(4px);
      color: #fff;
    }

    .nav-pill.active {
      background: linear-gradient(135deg, rgba(0,135,82,0.9), rgba(188,213,49,0.65));
      color: #fff;
      box-shadow: 0 12px 18px rgba(0, 135, 82, 0.25);
    }

    .app-content {
      flex: 1;
      padding: 2rem clamp(1.25rem, 1.5vw + 1rem, 2.75rem);
      display: flex;
      flex-direction: column;
      gap: 1.5rem;
    }

    .surface-card {
      background: var(--sg-surface);
      border-radius: 18px;
      border: 1px solid rgba(35, 31, 32, 0.06);
      box-shadow: var(--sg-shadow);
    }

    .stat-card {
      position: relative;
      overflow: hidden;
      padding: 1.5rem;
    }

    .stat-card::after {
      content: "";
      position: absolute;
      inset: auto -40% -50% auto;
      width: 220px;
      height: 220px;
      background: radial-gradient(circle, rgba(188,213,49,0.25), transparent 65%);
      transform: rotate(25deg);
    }

    .stat-card .stat-kicker {
      text-transform: uppercase;
      letter-spacing: 0.12em;
      font-size: 0.75rem;
      color: rgba(35, 31, 32, 0.58);
      margin-bottom: 0.35rem;
    }

    .stat-card .stat-value {
      font-size: clamp(2.2rem, 2.5vw + 1rem, 3.4rem);
      font-weight: 600;
      margin: 0;
    }

    .badge-chip {
      border-radius: 999px;
      padding: 0.35rem 0.85rem;
      font-weight: 600;
      font-size: 0.75rem;
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      letter-spacing: 0.04em;
    }

    .badge-chip i { font-size: 0.9rem; }

    .priority-high { background: rgba(197,48,48,0.2); color: #8f1e1e; }
    .priority-medium { background: rgba(188,213,49,0.28); color: #4d4f12; }
    .priority-low { background: rgba(35,31,32,0.12); color: var(--sg-black); }

    .tour-overlay {
      position: fixed;
      inset: 0;
      background: rgba(35,31,32,0.55);
      backdrop-filter: blur(2px);
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.25s ease;
      z-index: 1050;
    }

    .tour-overlay.active {
      opacity: 1;
      pointer-events: auto;
    }

    .tour-highlight {
      position: relative;
      z-index: 1056 !important;
      box-shadow: 0 0 0 4px rgba(188,213,49,0.55), 0 18px 35px rgba(0,0,0,0.25);
      border-radius: 16px;
      transition: box-shadow 0.25s ease;
    }

    .tour-tooltip {
      position: absolute;
      z-index: 1060;
      background: var(--sg-surface);
      border-radius: 18px;
      padding: 1.1rem 1.35rem;
      box-shadow: var(--sg-shadow);
      max-width: 320px;
      opacity: 0;
      transform: translateY(8px);
      transition: opacity 0.2s ease, transform 0.2s ease;
    }

    .tour-tooltip.active {
      opacity: 1;
      transform: translateY(0);
    }

    .tour-tooltip h5 {
      font-size: 1rem;
      margin-bottom: 0.35rem;
    }

    .tour-tooltip p {
      font-size: 0.9rem;
      margin-bottom: 0.85rem;
      color: rgba(35,31,32,0.72);
    }

    .tour-controls {
      display: flex;
      justify-content: flex-end;
      gap: 0.5rem;
    }

    .tour-progress {
      font-size: 0.72rem;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: rgba(35,31,32,0.6);
      font-weight: 600;
    }

    .tour-skip {
      background: none;
      border: none;
      font-size: 0.72rem;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: rgba(35,31,32,0.55);
      font-weight: 600;
    }

    .tour-skip:hover,
    .tour-skip:focus {
      color: var(--sg-green);
    }

    @media (max-width: 575.98px) {
      .tour-tooltip {
        max-width: calc(100vw - 2rem);
      }
    }

    .badge-open { background: rgba(0,135,82,0.12); color: var(--sg-green); }
    .badge-progress { background: rgba(188,213,49,0.15); color: #505115; }
    .badge-waiting { background: rgba(255,193,7,0.18); color: #8a6d00; }
    .badge-complete { background: rgba(0,135,82,0.2); color: var(--sg-green); }
    .badge-closed { background: rgba(35,31,32,0.12); color: var(--sg-black); }

    .filter-panel {
      background: rgba(255, 255, 255, 0.8);
      backdrop-filter: blur(12px);
      border-radius: 20px;
      border: 1px solid rgba(35,31,32,0.08);
      padding: 1.5rem;
      box-shadow: var(--sg-shadow);
    }

    .filter-pill {
      border-radius: 30px;
      border: 1px solid rgba(0,135,82,0.35);
      padding: 0.45rem 0.9rem;
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      font-size: 0.85rem;
      margin: 0.25rem 0.3rem 0 0;
    }

    .table-modern {
      border-collapse: separate;
      border-spacing: 0 0.75rem;
    }

    .table-modern thead th {
      text-transform: uppercase;
      font-size: 0.68rem;
      letter-spacing: 0.14em;
      color: rgba(35,31,32,0.55);
      border: none;
    }

    .table-modern tbody tr {
      box-shadow: var(--sg-shadow);
      border-radius: 18px;
    }

    .table-modern tbody td {
      background: var(--sg-surface);
      border: none;
      vertical-align: middle;
      padding: 1.1rem;
    }

    .table-modern tbody tr td:first-child { border-top-left-radius: 18px; border-bottom-left-radius: 18px; }
    .table-modern tbody tr td:last-child { border-top-right-radius: 18px; border-bottom-right-radius: 18px; }

    .ticket-title {
      font-weight: 600;
      margin-bottom: 0.25rem;
    }

    .ticket-meta {
      color: rgba(35,31,32,0.6);
      font-size: 0.82rem;
    }

    .timeline {
      position: relative;
      padding-left: 1.5rem;
    }

    .timeline::before {
      content: "";
      position: absolute;
      left: 0.4rem;
      top: 0.1rem;
      bottom: 0.1rem;
      width: 2px;
      background: linear-gradient(180deg, rgba(0,135,82,0.8), rgba(188,213,49,0.2));
    }

    .timeline-entry {
      position: relative;
      padding: 0.75rem 0 0.75rem 1.5rem;
    }

    .timeline-entry::before {
      content: "";
      position: absolute;
      left: -0.02rem;
      top: 1rem;
      width: 12px;
      height: 12px;
      border-radius: 999px;
      background: var(--sg-green);
      box-shadow: 0 0 0 6px rgba(0,135,82,0.15);
    }

    .btn-primary {
      background: var(--sg-green);
      border-color: var(--sg-green);
      border-radius: 999px;
      font-weight: 600;
      padding-inline: 1.3rem;
      transition: transform 0.18s ease, box-shadow 0.18s ease;
    }

    .btn-primary:hover {
      background: var(--sg-green-dark);
      border-color: var(--sg-green-dark);
      transform: translateY(-1px);
      box-shadow: 0 12px 18px rgba(0, 135, 82, 0.25);
    }

    .btn-outline-light {
      border-radius: 999px;
    }

    .form-control, .form-select {
      border-radius: 14px;
      border: 1px solid rgba(35,31,32,0.12);
      padding: 0.65rem 0.95rem;
      background: #fff;
    }

    .form-control:focus, .form-select:focus {
      border-color: rgba(0,135,82,0.65);
      box-shadow: 0 0 0 .25rem rgba(0,135,82,0.15);
    }

    .flash-message {
      border-radius: 16px;
      padding: 0.9rem 1.1rem;
      background: rgba(188,213,49,0.25);
      color: var(--sg-black);
      border: 1px solid rgba(188,213,49,0.45);
      font-weight: 500;
    }

    .skeleton {
      background: linear-gradient(90deg, rgba(220,224,217,0.4), rgba(220,224,217,0.75), rgba(220,224,217,0.4));
      background-size: 200% 100%;
      animation: shimmer 1.8s infinite;
    }

    @keyframes shimmer {
      0% { background-position: -150% 0; }
      100% { background-position: 150% 0; }
    }

    @media (max-width: 991px) {
      .app-shell { flex-direction: column; }
      .app-sidebar {
        width: 100%;
        height: auto;
        position: static;
        border-radius: 0 0 20px 20px;
        background: rgba(35,31,32,0.96);
        flex-direction: row;
        flex-wrap: wrap;
        justify-content: flex-start;
      }
      .nav-pill { flex: 1 1 45%; }
      .app-content { padding-top: 1.5rem; }
    }

    @media (max-width: 575px) {
      .app-header { flex-direction: column; gap: 0.75rem; align-items: flex-start; }
      .app-user-meta { width: 100%; justify-content: space-between; }
    }
  </style>
</head>
<body class="{% if request.endpoint != 'home' %}has-shell{% endif %}">
{% if request.endpoint != 'home' %}
  <header class="app-header">
    <a class="brand-mark text-white" href="{{ url_for('tickets') }}">
      <span class="brand-icon"><i class="bi bi-stars"></i></span>
      <span>Seegars IT Workspace</span>
    </a>
    <div class="app-user-meta">
      {% if session.get('user') %}
        <div class="text-white-50">Signed in as <strong>{{ session['user']['name'] or session['user']['email'] }}</strong></div>
        <a class="btn btn-outline-light btn-sm" href="{{ url_for('logout') }}">Logout</a>
        <a class="btn btn-primary btn-sm" href="{{ url_for('new_ticket') }}"><i class="bi bi-plus-lg me-1"></i>New Ticket</a>
      {% else %}
        <a class="btn btn-primary btn-sm" href="{{ url_for('login') }}">Login with Microsoft</a>
      {% endif %}
    </div>
  </header>
  <div class="app-shell">
    <aside class="app-sidebar">
      <div>
        <div class="nav-section-title">Workspace</div>
        <a class="nav-pill {% if request.endpoint == 'tickets' %}active{% endif %}" href="{{ url_for('tickets') }}"><i class="bi bi-speedometer"></i>Dashboard</a>
        <a class="nav-pill {% if request.endpoint == 'new_ticket' %}active{% endif %}" href="{{ url_for('new_ticket') }}"><i class="bi bi-plus-circle"></i>New Ticket</a>
        <a class="nav-pill {% if request.endpoint in ('kb_page', 'kb_article') %}active{% endif %}" href="{{ url_for('kb_page') }}"><i class="bi bi-life-preserver"></i>Knowledge Base</a>
        <a class="nav-pill {% if request.endpoint == 'feedback_analytics' %}active{% endif %}" href="{{ url_for('feedback_analytics') }}"><i class="bi bi-chat-dots"></i>Feedback</a>
      </div>
    </aside>
    <main class="app-content">
      {% with messages = get_flashed_messages() %}
        {% if messages %}
          <div class="flash-message">{{ messages|join('\n') }}</div>
        {% endif %}
      {% endwith %}
      {% block workspace_content %}{% endblock %}
    </main>
  </div>
{% else %}
  <main class="container py-5">
    {% with messages = get_flashed_messages() %}
      {% if messages %}
        <div class="flash-message mb-4">{{ messages|join('\n') }}</div>
      {% endif %}
    {% endwith %}
    {% block home_content %}{% endblock %}
  </main>
{% endif %}
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""


DASHBOARD_HTML = """
{% extends 'base.html' %}
{% block workspace_content %}
<section class="d-flex flex-wrap align-items-center justify-content-between gap-3">
  <div>
    <span class="badge-chip badge-open text-uppercase small"><i class="bi bi-broadcast-pin"></i> Live Queue</span>
    <h1 class="fw-semibold display-5 mb-2">Support Command Center</h1>
    <p class="text-secondary mb-0">Monitor ticket health, triage new issues, and keep Seegars teams moving.</p>
  </div>
  <div class="d-flex gap-2 flex-wrap">
    <a class="btn btn-primary d-flex align-items-center gap-2" href="{{ url_for('new_ticket') }}"><i class="bi bi-plus-lg"></i>New Ticket</a>
  </div>
</section>

<div class="row g-3">
  <div class="col-md-4 col-xl-3">
    <div class="surface-card stat-card h-100">
      <div class="stat-kicker">Total Volume{% if admin %} · All Users{% endif %}</div>
      <p class="stat-value">{{ stats.total }}</p>
      <div class="text-secondary d-flex align-items-center gap-2"><i class="bi bi-people"></i><span>Requests captured</span></div>
    </div>
  </div>
  <div class="col-md-4 col-xl-3">
    <div class="surface-card stat-card h-100">
      <div class="stat-kicker">Open Tickets</div>
      <p class="stat-value text-warning mb-1">{{ stats.open }}</p>
      <div class="text-secondary d-flex align-items-center gap-2"><i class="bi bi-activity"></i><span>Awaiting resolution</span></div>
    </div>
  </div>
  <div class="col-md-4 col-xl-3">
    <div class="surface-card stat-card h-100">
      <div class="stat-kicker">Completed</div>
      <p class="stat-value text-success mb-1">{{ stats.completed }}</p>
      <div class="text-secondary d-flex align-items-center gap-2"><i class="bi bi-check2-circle"></i><span>Resolved or closed</span></div>
    </div>
  </div>
  <div class="col-12 col-xl-3">
    <div class="surface-card p-4 h-100">
      <div class="d-flex align-items-center justify-content-between mb-2">
        <h6 class="mb-0 text-uppercase small text-secondary">Top Categories</h6>
        <span class="badge-chip badge-closed"><i class="bi bi-pie-chart"></i>Mix</span>
      </div>
      <div class="d-flex flex-column gap-2">
        {% for item in category_stats %}
          <div class="d-flex justify-content-between align-items-center">
            <span class="fw-semibold">{{ item.category }}</span>
            <span class="text-secondary">{{ item.count }}</span>
          </div>
        {% else %}
          <div class="text-secondary">No tickets yet.</div>
        {% endfor %}
      </div>
    </div>
  </div>
</div>

<div class="surface-card p-4">
  <form class="row g-3 align-items-end" method="get">
    <div class="col-12 col-md-6 col-xl-2">
      <label class="form-label text-uppercase small">Status</label>
      <select class="form-select" name="status">
        <option value="">All statuses</option>
        {% for s in statuses %}
        <option value="{{ s }}" {% if s == selected_status %}selected{% endif %}>{{ s }}</option>
        {% endfor %}
      </select>
    </div>
    <div class="col-12 col-md-6 col-xl-2">
      <label class="form-label text-uppercase small">Priority</label>
      <select class="form-select" name="priority">
        <option value="">All priorities</option>
        {% for p in priorities %}
        <option value="{{ p }}" {% if p == selected_priority %}selected{% endif %}>{{ p }}</option>
        {% endfor %}
      </select>
    </div>
    <div class="col-12 col-md-6 col-xl-2">
      <label class="form-label text-uppercase small">Branch</label>
      <select class="form-select" name="branch">
        <option value="">All branches</option>
        {% for b in branches %}
        <option value="{{ b }}" {% if b == selected_branch %}selected{% endif %}>{{ b }}</option>
        {% endfor %}
      </select>
    </div>
    <div class="col-12 col-md-6 col-xl-2">
      <label class="form-label text-uppercase small">Category</label>
      <select class="form-select" name="category">
        <option value="">All categories</option>
        {% for c in categories %}
        <option value="{{ c }}" {% if c == selected_category %}selected{% endif %}>{{ c }}</option>
        {% endfor %}
      </select>
    </div>
    <div class="col-12 col-md-6 col-xl-4 d-flex flex-wrap gap-2 justify-content-end">
      <button class="btn btn-primary d-flex align-items-center gap-2" type="submit"><i class="bi bi-funnel"></i>Filter</button>
      <a class="btn btn-link text-decoration-none" href="{{ url_for('tickets') }}">Clear</a>
    </div>
  </form>
</div>

<div class="surface-card p-0 overflow-hidden">
  <div class="d-flex flex-wrap align-items-center justify-content-between gap-2 p-4 border-bottom border-light-subtle">
    <div class="d-flex align-items-center gap-3 flex-wrap">
      <div class="badge-chip badge-open"><i class="bi bi-lightning"></i>{{ tickets|length }} Results</div>
      {% if filters_applied %}
      <div class="d-flex flex-wrap gap-2">
        {% for label, value in filters_applied %}
        <span class="badge bg-light text-dark border">{{ label }}: {{ value }}</span>
        {% endfor %}
      </div>
      {% else %}
      <span class="text-secondary small">Sorted by most recent updates</span>
      {% endif %}
    </div>
    <button type="button" class="btn btn-sm btn-outline-dark d-flex align-items-center gap-2"><i class="bi bi-cloud-download"></i>Export</button>
  </div>
  <div class="table-responsive p-3">
    <table class="table table-modern align-middle mb-0">
      <thead>
        <tr>
          <th scope="col">Submitted</th>
          {% if admin %}<th scope="col">Branch</th>{% endif %}
          <th scope="col">Ticket</th>
          <th scope="col">Priority</th>
          <th scope="col">Category</th>
          <th scope="col">Status</th>
          <th scope="col">Completed</th>
          {% if admin %}<th scope="col" class="text-end">Actions</th>{% endif %}
        </tr>
      </thead>
      <tbody>
        {% for t in tickets %}
        {% set status = t['status'] or 'Open' %}
        {% set status_style = status_badges.get(status, status_badges['Open']) %}
        {% set priority_label = (t['priority'] or 'Medium') %}
        {% set priority_style = priority_badges.get(priority_label, priority_badges['Medium']) %}
        <tr>
          <td><div class="fw-semibold">{{ format_ts(t['created_at']) }}</div><div class="ticket-meta">Updated {{ format_ts(t['updated_at']) }}</div></td>
          {% if admin %}
          <td><span class="ticket-title">{{ t['branch'] or '—' }}</span><div class="ticket-meta">{{ t['requester_name'] or 'Unknown' }}</div></td>
          {% endif %}
          <td>
            <div class="ticket-title">{{ t['title'] }}</div>
            <div class="ticket-meta">#{{ t['id'] }}{% if not admin and t['branch'] %} • {{ t['branch'] }}{% endif %}{% if admin and t['requester_email'] %} • {{ t['requester_email'] }}{% endif %}</div>
          </td>
          <td>
            <span class="{{ priority_style.cls }}"><i class="{{ priority_style.icon }}"></i>{{ priority_label }}</span>
          </td>
          <td>{{ t['category'] or '—' }}</td>
          <td>
            <span class="{{ status_style.cls }}"><i class="{{ status_style.icon }}"></i>{{ status }}</span>
          </td>
          <td>{{ format_ts(t['completed_at']) }}</td>
          {% if admin %}
          <td class="text-end">
            <form class="d-flex flex-wrap gap-2 justify-content-end align-items-center" method="post" action="{{ url_for('update_status', ticket_id=t['id']) }}">
              <select name="status" class="form-select form-select-sm" style="min-width: 160px;">
                {% for s in statuses %}
                <option value="{{ s }}" {% if s == status %}selected{% endif %}>{{ s }}</option>
                {% endfor %}
              </select>
              <button class="btn btn-sm btn-primary" type="submit"><i class="bi bi-arrow-repeat"></i></button>
              <a class="btn btn-sm btn-outline-dark" href="{{ url_for('ticket_detail', ticket_id=t['id']) }}">Details</a>
            </form>
          </td>
          {% endif %}
        </tr>
        {% else %}
        <tr>
          <td colspan="{% if admin %}8{% else %}7{% endif %}" class="text-center py-5">
            <div class="fw-semibold mb-2">No tickets yet</div>
            <p class="text-secondary mb-0">Create your first request to populate the workspace.</p>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</div>
{% endblock %}
"""


NEW_HTML = """
{% extends 'base.html' %}
{% block workspace_content %}
<div class="surface-card p-4 p-lg-5">
  <div class="d-flex flex-wrap align-items-start justify-content-between gap-3 mb-4">
    <div>
      <span class="badge-chip badge-open text-uppercase small"><i class="bi bi-pencil"></i> Guided form</span>
      <h2 class="fw-semibold mb-2">Submit a Support Ticket</h2>
      <p class="text-secondary mb-0">Tell us what’s happening and we’ll route it to the Seegars IT team instantly.</p>
    </div>
    <div class="d-flex align-items-center gap-2 text-secondary small">
      <i class="bi bi-shield-lock-fill text-success"></i>
      <span>Microsoft 365 secure workspace</span>
    </div>
  </div>

  {% if not session.get('user') %}
    <div class="alert alert-warning d-flex align-items-center gap-2"><i class="bi bi-info-circle"></i><span>Please <a href="{{ url_for('login') }}" class="fw-semibold">sign in with Microsoft</a> to submit a ticket.</span></div>
  {% endif %}

  <div class="d-flex flex-wrap gap-2 mb-4">
    <span class="filter-pill"><i class="bi bi-1-circle"></i>Describe issue</span>
    <span class="filter-pill"><i class="bi bi-2-circle"></i>Contact & location</span>
    <span class="filter-pill"><i class="bi bi-3-circle"></i>Review & submit</span>
  </div>

  <form method="post" enctype="multipart/form-data" class="d-flex flex-column gap-4">
    <div class="row g-4">
      <div class="col-lg-8">
        <div class="d-flex flex-column gap-3">
          <div>
            <label class="form-label fw-semibold">Title</label>
            <input name="title" class="form-control" placeholder="Example: AccountMate won’t open" required data-tour-step="title">
          </div>
          <div>
            <label class="form-label fw-semibold">What’s happening?</label>
            <textarea name="description" class="form-control" rows="6" placeholder="Share clear details, steps, and any error messages." required data-tour-step="description"></textarea>
          </div>
          <div class="row g-3" data-tour-step="contact">
            <div class="col-md-6">
              <label class="form-label fw-semibold">Your name</label>
              <input name="requester_name" class="form-control" placeholder="First and last name">
            </div>
            <div class="col-md-6">
              <label class="form-label fw-semibold">Email</label>
              <input type="email" name="requester_email" class="form-control" placeholder="you@seegarsfence.com">
            </div>
          </div>
        </div>
      </div>
      <div class="col-lg-4">
        <div class="surface-card p-3 p-lg-4">
          <h5 class="fw-semibold mb-3">Triage details</h5>
          <div class="mb-3">
            <label class="form-label text-uppercase small">Priority</label>
            <select name="priority" class="form-select" data-tour-step="priority">
              {% for option in priorities %}
              <option value="{{ option }}" {% if option == 'Medium' %}selected{% endif %}>{{ option }}</option>
              {% endfor %}
            </select>
          </div>
          <div class="mb-3" data-tour-step="branch">
            <label class="form-label text-uppercase small">Branch</label>
            <select name="branch" class="form-select">
              <option value="">Select a branch…</option>
              {% for branch in branches %}
              <option value="{{ branch }}">{{ branch }}</option>
              {% endfor %}
            </select>
          </div>
          <div class="mb-3">
            <label class="form-label text-uppercase small">Category</label>
            <select name="category" class="form-select">
              <option value="">Choose a category…</option>
              {% for cat in categories %}
              <option value="{{ cat }}">{{ cat }}</option>
              {% endfor %}
            </select>
          </div>
          <div class="mb-3" data-tour-step="attachments">
            <label class="form-label text-uppercase small">Attachments</label>
            <input type="file" id="attachments" name="attachments" class="form-control" multiple hidden>
            <div class="d-flex flex-wrap align-items-center gap-2">
              <label for="attachments" class="btn btn-outline-dark btn-sm d-flex align-items-center gap-2 mb-0">
                <i class="bi bi-paperclip"></i>
                Add attachments
              </label>
              <span class="text-secondary small">Total size up to {{ attachment_limit }}.</span>
            </div>
            <ul class="list-unstyled small text-secondary mt-2 mb-0" id="attachment-list"></ul>
          </div>
        </div>
      </div>
    </div>
    <div class="d-flex flex-wrap gap-3 justify-content-end">
      <a class="btn btn-outline-dark" href="{{ url_for('tickets') }}">Cancel</a>
      <button class="btn btn-primary d-flex align-items-center gap-2" type="submit" data-tour-step="submit"><i class="bi bi-send"></i>Submit ticket</button>
    </div>
  </form>
  <script>
    (function () {
      const attachmentInput = document.getElementById('attachments');
      const attachmentList = document.getElementById('attachment-list');
      if (!attachmentInput || !attachmentList) {
        return;
      }
      attachmentInput.addEventListener('change', () => {
        attachmentList.innerHTML = '';
        const { files } = attachmentInput;
        if (!files || files.length === 0) {
          return;
        }
        Array.from(files).forEach((file) => {
          const item = document.createElement('li');
          item.className = 'd-flex align-items-center gap-2';
          const icon = document.createElement('i');
          icon.className = 'bi bi-file-earmark';
          const text = document.createElement('span');
          text.textContent = file.name;
          item.append(icon, text);
          attachmentList.appendChild(item);
        });
      });
    })();

    (function () {
      const user = {{ (session.get('user') or {})|tojson }};
      if (!user || !user.email) {
        return;
      }
      const storageKey = `seegars-ticket-tour:${String(user.email).toLowerCase()}`;
      const params = new URLSearchParams(window.location.search);
      const forceTour = params.get('tour') === '1';

      let storageAvailable = true;
      try {
        const probeKey = '__seegars_ticket_tour_probe__';
        localStorage.setItem(probeKey, '1');
        localStorage.removeItem(probeKey);
      } catch (err) {
        storageAvailable = false;
      }

      let seenAlready = false;
      if (storageAvailable) {
        try {
          seenAlready = Boolean(localStorage.getItem(storageKey));
        } catch (err) {
          seenAlready = false;
        }
      }

      if (!forceTour && seenAlready) {
        return;
      }

      const steps = [
        {
          selector: "[data-tour-step='title']",
          title: 'Title your request',
          text: 'Start with a short subject so the IT team can immediately recognize the problem.'
        },
        {
          selector: "[data-tour-step='description']",
          title: 'Explain what’s happening',
          text: 'Share the symptoms, steps you have tried, and any error messages so we can help faster.'
        },
        {
          selector: "[data-tour-step='contact']",
          title: 'Let us know who to reach',
          text: 'Add your name and email so updates and resolutions go straight to you.'
        },
        {
          selector: "[data-tour-step='priority']",
          title: 'Set the urgency',
          text: 'Pick the priority that matches business impact. We will triage based on what you choose.'
        },
        {
          selector: "[data-tour-step='branch']",
          title: 'Tell us where you are',
          text: 'Select your branch so the right regional resources are looped in automatically.'
        },
        {
          selector: "[data-tour-step='attachments']",
          title: 'Add supporting files',
          text: 'Screenshots, logs, or documents can be attached here to give the team extra context.'
        },
        {
          selector: "[data-tour-step='submit']",
          title: 'Submit your ticket',
          text: 'When everything looks good, send it to Seegars IT and we’ll get to work right away.'
        }
      ];

      let overlay;
      let tooltip;
      let progressEl;
      let titleEl;
      let textEl;
      let backBtn;
      let nextBtn;
      let skipBtn;
      let currentIndex = -1;
      let currentTarget = null;
      let rafHandle = 0;

      function buildUi() {
        overlay = document.createElement('div');
        overlay.className = 'tour-overlay';
        tooltip = document.createElement('div');
        tooltip.className = 'tour-tooltip';
        tooltip.innerHTML = `
          <div class="d-flex justify-content-between align-items-center mb-2">
            <span class="tour-progress"></span>
            <button type="button" class="tour-skip">Skip tour</button>
          </div>
          <h5 class="tour-title mb-1"></h5>
          <p class="tour-text mb-0"></p>
          <div class="tour-controls mt-3">
            <button type="button" class="btn btn-outline-dark btn-sm tour-back">Back</button>
            <button type="button" class="btn btn-primary btn-sm tour-next">Next</button>
          </div>
        `;
        document.body.append(overlay, tooltip);

        progressEl = tooltip.querySelector('.tour-progress');
        titleEl = tooltip.querySelector('.tour-title');
        textEl = tooltip.querySelector('.tour-text');
        backBtn = tooltip.querySelector('.tour-back');
        nextBtn = tooltip.querySelector('.tour-next');
        skipBtn = tooltip.querySelector('.tour-skip');

        backBtn.addEventListener('click', () => goToStep(currentIndex - 1));
        nextBtn.addEventListener('click', () => {
          if (currentIndex >= steps.length - 1) {
            endTour(true);
          } else {
            goToStep(currentIndex + 1);
          }
        });

        const skipTour = () => endTour(true);
        skipBtn.addEventListener('click', skipTour);
        overlay.addEventListener('click', skipTour);
      }

      function refreshPosition() {
        if (!currentTarget) {
          return;
        }
        if (rafHandle) {
          cancelAnimationFrame(rafHandle);
        }
        rafHandle = requestAnimationFrame(() => {
          positionTooltip(currentTarget);
        });
      }

      function positionTooltip(target) {
        const rect = target.getBoundingClientRect();
        const tooltipRect = tooltip.getBoundingClientRect();
        const viewportTop = window.scrollY + 20;
        const viewportBottom = window.scrollY + window.innerHeight - 20;

        let top = rect.bottom + 16 + window.scrollY;
        let left = rect.left + rect.width / 2 - tooltipRect.width / 2 + window.scrollX;

        if (top + tooltipRect.height > viewportBottom) {
          top = rect.top + window.scrollY - tooltipRect.height - 16;
        }

        if (top < viewportTop) {
          top = viewportTop;
        }

        const minLeft = window.scrollX + 20;
        const maxLeft = window.scrollX + window.innerWidth - tooltipRect.width - 20;
        if (left < minLeft) {
          left = minLeft;
        } else if (left > maxLeft) {
          left = maxLeft;
        }

        tooltip.style.top = `${top}px`;
        tooltip.style.left = `${left}px`;
      }

      function clearHighlight() {
        if (!currentTarget) {
          return;
        }
        currentTarget.classList.remove('tour-highlight');
        currentTarget.removeAttribute('data-tour-active');
        currentTarget = null;
      }

      function goToStep(index) {
        if (index < 0) {
          index = 0;
        }
        if (index >= steps.length) {
          endTour(true);
          return;
        }

        const step = steps[index];
        const target = document.querySelector(step.selector);

        if (!target) {
          const direction = index > currentIndex ? 1 : -1;
          const nextIndex = index + direction;
          if (nextIndex >= 0 && nextIndex < steps.length) {
            goToStep(nextIndex);
            return;
          }
          endTour(true);
          return;
        }

        clearHighlight();

        currentIndex = index;
        currentTarget = target;
        currentTarget.setAttribute('data-tour-active', 'true');
        currentTarget.classList.add('tour-highlight');

        progressEl.textContent = `Step ${index + 1} of ${steps.length}`;
        titleEl.textContent = step.title;
        textEl.textContent = step.text;
        backBtn.disabled = index === 0;
        nextBtn.textContent = index === steps.length - 1 ? 'Finish' : 'Next';

        overlay.classList.add('active');
        tooltip.classList.add('active');

        currentTarget.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'center' });
        refreshPosition();
        setTimeout(refreshPosition, 350);
      }

      function endTour(markSeen) {
        window.removeEventListener('resize', refreshPosition, true);
        window.removeEventListener('scroll', refreshPosition, true);
        clearHighlight();
        overlay.classList.remove('active');
        tooltip.classList.remove('active');
        setTimeout(() => {
          overlay.remove();
          tooltip.remove();
        }, 220);
        if (markSeen && storageAvailable) {
          try {
            localStorage.setItem(storageKey, String(Date.now()));
          } catch (err) {
            /* no-op */
          }
        }
      }

      function startTour() {
        if (!steps.length) {
          return;
        }
        buildUi();
        tooltip.style.visibility = 'hidden';
        goToStep(0);
        tooltip.style.visibility = '';
        window.addEventListener('resize', refreshPosition, true);
        window.addEventListener('scroll', refreshPosition, true);
      }

      if (document.readyState === 'complete') {
        setTimeout(startTour, forceTour ? 50 : 350);
      } else {
        window.addEventListener('load', () => setTimeout(startTour, forceTour ? 50 : 350));
      }
    })();
  </script>
</div>
{% endblock %}
"""




DETAIL_HTML = """
{% extends 'base.html' %}
{% block workspace_content %}
{% set status = t['status'] or 'Open' %}
{% set status_style = status_badges.get(status, status_badges['Open']) %}
{% set priority_label = t['priority'] or 'Medium' %}
{% set priority_style = priority_badges.get(priority_label, priority_badges['Medium']) %}
<div class="d-flex flex-wrap align-items-start justify-content-between gap-3 mb-4">
  <div>
    <span class="badge-chip badge-open text-uppercase small"><i class="bi bi-ticket-detailed"></i> Ticket #{{ t['id'] }}</span>
    <h2 class="fw-semibold mt-2 mb-2">{{ t['title'] }}</h2>
    <div class="d-flex flex-wrap gap-2 text-secondary small">
      <span><i class="bi bi-person-circle me-1"></i>{{ t['requester_name'] or 'Anonymous' }}</span>
      {% if t['requester_email'] %}<span>• <i class="bi bi-envelope me-1"></i>{{ t['requester_email'] }}</span>{% endif %}
      {% if t['branch'] %}<span>• <i class="bi bi-geo-alt me-1"></i>{{ t['branch'] }}</span>{% endif %}
      <span>• Created {{ format_ts(t['created_at']) }}</span>
    </div>
  </div>
  <div class="d-flex flex-wrap gap-2">
    <span class="{{ status_style.cls }}"><i class="{{ status_style.icon }}"></i>{{ status }}</span>
    <span class="{{ priority_style.cls }}"><i class="{{ priority_style.icon }}"></i>{{ priority_label }} Priority</span>
  </div>
</div>

<div class="row g-4">
  <div class="col-xl-8">
    <div class="surface-card p-4">
      <div class="d-flex align-items-center justify-content-between mb-3">
        <h5 class="fw-semibold mb-0">Issue details</h5>
        <span class="badge-chip badge-closed"><i class="bi bi-tag"></i>{{ t['category'] or 'Uncategorized' }}</span>
      </div>
      <p class="mb-0" style="white-space: pre-line;">{{ t['description'] }}</p>
      <div class="d-flex flex-wrap gap-3 text-secondary small mt-4">
        <span><i class="bi bi-clock-history me-1"></i>Updated {{ format_ts(t['updated_at']) }}</span>
        <span><i class="bi bi-calendar-check me-1"></i>Completed {{ format_ts(t['completed_at']) }}</span>
        <span><i class="bi bi-person-bounding-box me-1"></i>Assigned to {{ t['assignee'] or 'Unassigned' }}</span>
      </div>
    </div>

    <div class="surface-card p-4 mt-4">
      <div class="d-flex align-items-center justify-content-between mb-3">
        <h5 class="fw-semibold mb-0">Activity timeline</h5>
        <span class="text-secondary small">Automatic history & comments</span>
      </div>
      <div class="timeline">
        <div class="timeline-entry">
          <div class="fw-semibold">Ticket created</div>
          <div class="text-secondary small">{{ format_ts(t['created_at']) }} • {{ t['requester_name'] or 'Anonymous' }}</div>
        </div>
        {% for c in comments %}
        <div class="timeline-entry">
          <div class="fw-semibold d-flex align-items-center gap-2"><i class="bi bi-chat-dots text-success"></i>{{ c['author'] or 'Anonymous' }}</div>
          <div class="text-secondary small">{{ format_ts(c['created_at']) }}</div>
          <div class="mt-2" style="white-space: pre-line;">{{ c['body'] }}</div>
        </div>
        {% else %}
        <div class="timeline-entry">
          <div class="fw-semibold text-secondary">No comments yet</div>
          <div class="text-secondary small">Collaborate with your team to resolve this request.</div>
        </div>
        {% endfor %}
      </div>
      <div class="mt-4">
        <form method="post" action="{{ url_for('add_comment', ticket_id=t['id']) }}" class="surface-card p-3 border-0">
          <div class="row g-3 align-items-start">
            <div class="col-md-3">
              <label class="form-label text-uppercase small">Your name</label>
              <input class="form-control" name="author" placeholder="Optional">
            </div>
            <div class="col-md-9">
              <label class="form-label text-uppercase small">Comment</label>
              <textarea class="form-control" name="body" rows="3" placeholder="Add an update for the team" required></textarea>
            </div>
          </div>
          <div class="d-flex justify-content-end mt-3">
            <button class="btn btn-primary d-flex align-items-center gap-2" type="submit"><i class="bi bi-chat-text"></i>Post update</button>
          </div>
        </form>
      </div>
    </div>
  </div>

  <div class="col-xl-4">
    <div class="surface-card p-4 mb-4">
      <h5 class="fw-semibold mb-3">Requester info</h5>
      <div class="d-flex flex-column gap-2 text-secondary small">
        <div><i class="bi bi-person-fill me-2"></i>{{ t['requester_name'] or 'Not provided' }}</div>
        <div><i class="bi bi-envelope me-2"></i>{{ t['requester_email'] or 'Not provided' }}</div>
        <div><i class="bi bi-geo-alt me-2"></i>{{ t['branch'] or 'No branch set' }}</div>
      </div>
    </div>
    <div class="surface-card p-4 mb-4">
      <h5 class="fw-semibold mb-3">Attachments</h5>
      {% if attachments %}
      <ul class="list-unstyled d-flex flex-column gap-2 mb-0">
        {% for file in attachments %}
        <li class="d-flex justify-content-between align-items-center gap-3">
          <a class="d-flex align-items-center gap-2" href="{{ url_for('download_attachment', ticket_id=t['id'], attachment_id=file['id']) }}">
            <i class="bi bi-paperclip"></i>
            <span>{{ file['filename'] }}</span>
          </a>
          <span class="text-secondary small">{{ file['size_label'] }}</span>
        </li>
        {% endfor %}
      </ul>
      {% else %}
      <p class="text-secondary small mb-0">No attachments uploaded for this ticket.</p>
      {% endif %}
    </div>
    {% if admin %}
    <div class="surface-card p-4 mb-4">
      <h5 class="fw-semibold mb-3">Requester feedback</h5>
      {% if feedback_entries %}
      <div class="d-flex flex-column gap-3">
        {% for fb in feedback_entries %}
        <div class="p-3 border rounded bg-body-tertiary">
          <div class="d-flex justify-content-between align-items-center mb-2">
            <div class="fw-semibold small">Rating: {{ fb.rating or 'N/A' }}{% if fb.rating %}/5{% endif %}</div>
            <div class="text-secondary small">{{ format_ts(fb.submitted_at) }}</div>
          </div>
          <p class="mb-1">{{ fb.comments or 'No comments provided.' }}</p>
          {% if fb.submitted_by %}
          <div class="text-secondary small">— {{ fb.submitted_by }}</div>
          {% endif %}
        </div>
        {% endfor %}
      </div>
      {% else %}
      <p class="text-secondary small mb-0">No feedback submitted yet.</p>
      {% endif %}
    </div>
    {% endif %}
    <div class="surface-card p-4">
      <h5 class="fw-semibold mb-3">Ticket controls</h5>
      {% if admin %}
      <form method="post" action="{{ url_for('update_status', ticket_id=t['id']) }}" class="mb-3">
        <label class="form-label text-uppercase small">Status</label>
        <div class="d-flex gap-2">
          <select name="status" class="form-select">
            {% for s in statuses %}<option value="{{ s }}" {% if s == status %}selected{% endif %}>{{ s }}</option>{% endfor %}
          </select>
          <button class="btn btn-primary" type="submit"><i class="bi bi-arrow-repeat"></i></button>
        </div>
      </form>
      <form method="post" action="{{ url_for('update_assignee', ticket_id=t['id']) }}">
        <label class="form-label text-uppercase small">Assignee</label>
        <div class="d-flex gap-2">
          <input class="form-control" name="assignee" value="{{ t['assignee'] or '' }}" placeholder="e.g., Brad Wells">
          <button class="btn btn-outline-dark" type="submit">Save</button>
        </div>
      </form>
      {% else %}
      <p class="text-secondary small mb-0">Ticket updates are managed by the Seegars IT administrators. Reach out if you need to escalate an issue.</p>
      {% endif %}
    </div>
  </div>
</div>
{% endblock %}
"""


FEEDBACK_ANALYTICS_HTML = """
{% extends 'base.html' %}
{% block workspace_content %}
<section class="d-flex flex-wrap align-items-center justify-content-between gap-3">
  <div>
    <span class="badge-chip badge-open text-uppercase small"><i class="bi bi-chat-dots"></i> Feedback Insights</span>
    <h1 class="fw-semibold display-6 mb-2">Experience Report</h1>
    <p class="text-secondary mb-0">Review requester sentiment, track ratings, and spot trends from recent completions.</p>
  </div>
</section>

<div class="surface-card p-4">
  <form class="row g-3 align-items-end" method="get">
    <div class="col-12 col-md-4 col-xl-3">
      <label class="form-label text-uppercase small">Requester</label>
      <select class="form-select" name="requester_email">
        <option value="">All requesters</option>
        {% for requester in requester_options %}
        <option value="{{ requester }}" {% if requester == selected_requester %}selected{% endif %}>{{ requester }}</option>
        {% endfor %}
      </select>
    </div>
    <div class="col-12 col-md-4 col-xl-3">
      <label class="form-label text-uppercase small">Branch</label>
      <select class="form-select" name="branch">
        <option value="">All branches</option>
        {% for branch in branches %}
        <option value="{{ branch }}" {% if branch == selected_branch %}selected{% endif %}>{{ branch }}</option>
        {% endfor %}
      </select>
    </div>
    <div class="col-12 col-md-4 col-xl-3">
      <label class="form-label text-uppercase small">Category</label>
      <select class="form-select" name="category">
        <option value="">All categories</option>
        {% for category in categories %}
        <option value="{{ category }}" {% if category == selected_category %}selected{% endif %}>{{ category }}</option>
        {% endfor %}
      </select>
    </div>
    <div class="col-12 col-md-4 col-xl-3 d-flex flex-wrap gap-2 justify-content-end">
      <button class="btn btn-primary d-flex align-items-center gap-2" type="submit"><i class="bi bi-funnel"></i>Filter</button>
      <a class="btn btn-link text-decoration-none" href="{{ url_for('feedback_analytics') }}">Clear</a>
    </div>
  </form>
</div>

<div class="surface-card p-4">
  <div class="row g-3 align-items-center">
    <div class="col-md-4">
      <div class="stat-kicker">Average Rating</div>
      <div class="display-5 fw-semibold mb-0">{{ avg_rating }}</div>
    </div>
    <div class="col-md-4">
      <div class="stat-kicker">Total Feedbacks</div>
      <div class="h2 fw-semibold mb-0">{{ total_feedbacks }}</div>
    </div>
    <div class="col-md-4">
      <div class="stat-kicker">Ratings by star</div>
      <div class="d-flex flex-wrap gap-2 justify-content-md-end">
        {% for star in [5,4,3,2,1] %}
        <span class="badge bg-light text-dark border">{{ star }}★ {{ star_counts.get(star, 0) }}</span>
        {% endfor %}
      </div>
    </div>
  </div>
</div>

<div class="surface-card p-0 overflow-hidden">
  <div class="d-flex flex-wrap align-items-center justify-content-between gap-2 p-4 border-bottom border-light-subtle">
    <div class="badge-chip badge-open"><i class="bi bi-chat-square-text"></i>{{ entries|length }} Results</div>
    {% if filters_applied %}
    <div class="d-flex flex-wrap gap-2">
      {% for label, value in filters_applied %}
      <span class="badge bg-light text-dark border">{{ label }}: {{ value }}</span>
      {% endfor %}
    </div>
    {% endif %}
  </div>
  <div class="table-responsive p-3">
    <table class="table table-modern align-middle mb-0">
      <thead>
        <tr>
          <th scope="col">Submitted</th>
          <th scope="col">Ticket #</th>
          <th scope="col">Title</th>
          <th scope="col">Rating</th>
          <th scope="col">Comments</th>
          <th scope="col">Submitted By</th>
          <th scope="col">Branch</th>
          <th scope="col">Category</th>
        </tr>
      </thead>
      <tbody>
        {% for entry in entries %}
        <tr>
          <td>{{ format_ts(entry.submitted_at) }}</td>
          <td><a href="{{ url_for('ticket_detail', ticket_id=entry.ticket_id) }}">#{{ entry.ticket_id }}</a></td>
          <td>{{ entry.title or '—' }}</td>
          <td>{% if entry.rating %}{{ entry.rating }}/5{% else %}—{% endif %}</td>
          <td>{{ entry.comments or '—' }}</td>
          <td>{{ entry.submitted_by or '—' }}</td>
          <td>{{ entry.branch or '—' }}</td>
          <td>{{ entry.category or '—' }}</td>
        </tr>
        {% else %}
        <tr>
          <td colspan="8" class="text-center py-5">
            <div class="fw-semibold mb-2">No feedback yet</div>
            <p class="text-secondary mb-0">Complete tickets and request feedback to see insights here.</p>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</div>
{% endblock %}
"""


FEEDBACK_HTML = """
{% extends 'base.html' %}
{% block workspace_content %}
<div class="row justify-content-center">
  <div class="col-lg-7 col-xl-6">
    <div class="surface-card p-4 p-md-5">
      {% if ticket %}
        <span class="badge-chip badge-open text-uppercase small mb-2"><i class="bi bi-stars"></i> Ticket {{ ticket.id }}</span>
        <h1 class="fw-semibold mb-3">Share your feedback</h1>
        <p class="text-secondary">We appreciate you taking a moment to let us know how the Seegars IT team handled "{{ ticket.title }}".</p>
      {% else %}
        <h1 class="fw-semibold mb-3">Share your feedback</h1>
      {% endif %}
      {% if error %}
        <div class="alert alert-danger" role="alert">{{ error }}</div>
      {% endif %}
      {% if submitted %}
        <div class="text-center py-4">
          <div class="display-6 text-success mb-3"><i class="bi bi-emoji-smile"></i></div>
          <h2 class="fw-semibold">Thank you!</h2>
          <p class="text-secondary">Your feedback helps us improve our support experience.</p>
          {% if ticket_link %}
          <a class="btn btn-outline-dark" href="{{ ticket_link }}">Return to ticket</a>
          {% endif %}
        </div>
      {% elif show_form %}
        <form method="post" class="d-flex flex-column gap-3">
          <input type="hidden" name="token" value="{{ token }}">
          <div>
            <label class="form-label text-uppercase small">Overall experience</label>
            <select name="rating" class="form-select">
              <option value="">Choose a rating</option>
              {% for value, label in rating_choices %}
              <option value="{{ value }}" {% if rating|string == value|string %}selected{% endif %}>{{ value }} – {{ label }}</option>
              {% endfor %}
            </select>
          </div>
          <div>
            <label class="form-label text-uppercase small">Comments</label>
            <textarea name="comments" class="form-control" rows="4" placeholder="Tell us what worked well or what we can improve">{{ comments or '' }}</textarea>
          </div>
          <div>
            <label class="form-label text-uppercase small">Your name (optional)</label>
            <input name="submitted_by" class="form-control" placeholder="e.g., Alex Johnson" value="{{ submitted_by or '' }}">
          </div>
          <div class="d-flex justify-content-end gap-2">
            {% if ticket_link %}
            <a class="btn btn-outline-dark" href="{{ ticket_link }}">Back to ticket</a>
            {% endif %}
            <button class="btn btn-primary" type="submit"><i class="bi bi-send"></i> Submit feedback</button>
          </div>
        </form>
      {% else %}
        <div class="text-center py-4">
          <div class="display-6 text-warning mb-3"><i class="bi bi-exclamation-triangle"></i></div>
          <p class="text-secondary">This feedback link is no longer available. Please contact Seegars IT if you need assistance.</p>
          {% if ticket_link %}
          <a class="btn btn-outline-dark" href="{{ ticket_link }}">Go to ticket</a>
          {% endif %}
        </div>
      {% endif %}
    </div>
  </div>
</div>
{% endblock %}
"""


HOME_HTML = """
{% extends 'base.html' %}
{% block home_content %}
<div class="row g-4 align-items-center">
  <div class="col-lg-7">
    <span class="badge-chip badge-open text-uppercase small"><i class="bi bi-stars"></i> Seegars IT</span>
    <h1 class="display-4 fw-semibold mt-3 mb-3">Seegars IT Workspace</h1>
    <p class="lead text-secondary mb-4">Log, track, and resolve technology issues with the same polish as enterprise ticketing suites — all tuned to Seegars Fence Company’s workflow.</p>
    <ul class="list-unstyled d-flex flex-column gap-2 text-secondary">
      <li><i class="bi bi-check-circle-fill text-success me-2"></i>Secure Microsoft 365 authentication keeps requests protected.</li>
      <li><i class="bi bi-check-circle-fill text-success me-2"></i>Live dashboards give IT teams instant visibility into workload.</li>
      <li><i class="bi bi-check-circle-fill text-success me-2"></i>Smart triage fields route the right work to the right experts.</li>
    </ul>
    <div class="d-flex flex-wrap gap-3 mt-4">
      {% if session.get('user') %}
        <a class="btn btn-primary btn-lg d-flex align-items-center gap-2" href="{{ url_for('new_ticket') }}"><i class="bi bi-plus-lg"></i>New Ticket</a>
        <a class="btn btn-outline-dark d-flex align-items-center gap-2" href="{{ url_for('tickets') }}"><i class="bi bi-speedometer2"></i>Open dashboard</a>
      {% else %}
        <a class="btn btn-primary btn-lg d-flex align-items-center gap-2" href="{{ url_for('login') }}"><i class="bi bi-box-arrow-in-right"></i>Sign in with Microsoft</a>
      {% endif %}
    </div>
  </div>
  <div class="col-lg-5">
    <div class="surface-card p-4 p-lg-5 h-100">
      <h5 class="fw-semibold mb-3">What’s new?</h5>
      <div class="d-flex flex-column gap-3 text-secondary">
        <div class="d-flex gap-3">
          <div class="badge-chip badge-progress"><i class="bi bi-layout-sidebar"></i>Workspace shell</div>
          <p class="mb-0">A persistent navigation experience inspired by premium ticketing tools.</p>
        </div>
        <div class="d-flex gap-3">
          <div class="badge-chip badge-complete"><i class="bi bi-bar-chart"></i>Insight cards</div>
          <p class="mb-0">Track totals, open work, and completions at a glance.</p>
        </div>
        <div class="d-flex gap-3">
          <div class="badge-chip badge-waiting"><i class="bi bi-funnel"></i>Advanced filters</div>
          <p class="mb-0">Plan saved views and automation rules tailored to each branch.</p>
        </div>
      </div>
    </div>
  </div>
</div>
{% endblock %}
"""


KB_HTML = """
{% extends 'base.html' %}
{% block workspace_content %}
<section class="d-flex flex-column gap-4">
  <div class="row g-4">
    <div class="col-lg-8 col-xl-9 d-flex flex-column gap-4">
      <div class="surface-card p-4 p-md-5">
        <div class="d-flex flex-column flex-md-row gap-3 justify-content-between align-items-md-center">
          <div>
            <span class="badge-chip badge-open text-uppercase small"><i class="bi bi-life-preserver"></i> Knowledge Base</span>
            <h1 class="fw-semibold display-6 mt-3 mb-2">Instant Answers</h1>
            <p class="text-secondary mb-0">Search proven resolutions from closed tickets before opening something new.</p>
          </div>
          <a class="btn btn-outline-dark" href="{{ url_for('new_ticket') }}"><i class="bi bi-plus-circle"></i> Submit Ticket</a>
        </div>
        <form id="kb-search-form" class="mt-4" autocomplete="off">
          <div class="input-group input-group-lg">
            <span class="input-group-text bg-white border-end-0"><i class="bi bi-search"></i></span>
            <input class="form-control border-start-0" id="kb-query" name="query" placeholder="Ask a question…" aria-label="Ask a question…">
            <button class="btn btn-primary" type="submit"><i class="bi bi-stars me-1"></i>Ask</button>
          </div>
        </form>
        <p class="text-secondary small mt-2 mb-0">Try phrases like "update Sage password" or "printer offline".</p>
      </div>

      <div id="kb-answer-card" class="surface-card p-4 p-md-5 d-none">
        <div class="d-flex justify-content-between align-items-start gap-2 mb-3">
          <h2 class="h5 fw-semibold mb-0">AI Answer</h2>
          <span class="badge bg-light text-dark border">Preview</span>
        </div>
        <div id="kb-answer-text" class="text-secondary"></div>
        <div id="kb-answer-sources" class="small text-muted mt-3"></div>
        <div id="kb-answer-cta" class="alert alert-warning mt-4 d-none" role="alert">
          Still need help? <a class="alert-link" href="{{ url_for('new_ticket') }}">Create a support ticket</a> so the team can jump in.
        </div>
      </div>

      <div class="surface-card p-4 p-md-5">
        <div class="d-flex justify-content-between align-items-center flex-wrap gap-3 mb-4">
          <h2 class="h5 fw-semibold mb-0">Latest Knowledge Articles</h2>
          <span class="text-secondary small">Mark helpful solutions so they rise to the top.</span>
        </div>
        <div class="d-flex flex-column gap-3">
          {% if articles %}
            {% for article in articles %}
            <article class="border rounded-4 p-3 p-md-4">
              <div class="d-flex flex-column flex-md-row justify-content-between gap-3">
                <div>
                  <a class="h5 d-block mb-2" href="{{ url_for('kb_article', article_id=article.id) }}">{{ article.title }}</a>
                  <p class="text-secondary mb-2">{{ article.summary }}</p>
                  {% if article.tags %}
                  <div class="d-flex flex-wrap gap-2">
                    {% for tag in article.tags.split(',') if tag.strip() %}
                    <span class="badge bg-light text-dark border">{{ tag.strip() }}</span>
                    {% endfor %}
                  </div>
                  {% endif %}
                </div>
                <div class="text-secondary small text-md-end">
                  <div><i class="bi bi-hand-thumbs-up"></i> {{ article.helpful_up or 0 }} helpful</div>
                  <div><i class="bi bi-hand-thumbs-down"></i> {{ article.helpful_down or 0 }} not helpful</div>
                </div>
              </div>
            </article>
            {% endfor %}
          {% else %}
          <div class="alert alert-info mb-0" role="alert">
            No knowledge base content yet. Close a few tickets with solid write-ups to seed the library.
          </div>
          {% endif %}
        </div>
      </div>
    </div>
    {% if recent_queries %}
    <div class="col-lg-4 col-xl-3">
      <div class="surface-card p-4 h-100">
        <div class="d-flex justify-content-between align-items-center mb-3">
          <h5 class="mb-0">Recent Questions (admin)</h5>
          <span class="badge bg-light text-dark border">{{ recent_queries|length }}</span>
        </div>
        <div class="d-flex flex-column gap-3">
          {% for item in recent_queries %}
          <div class="border rounded-4 p-3">
            <div class="fw-semibold small mb-1">{{ item.query }}</div>
            <div class="text-secondary small">{{ format_ts(item.created_at) }}</div>
            {% if item.user_email %}
            <div class="text-secondary small">{{ item.user_email }}</div>
            {% endif %}
          </div>
          {% endfor %}
        </div>
      </div>
    </div>
    {% endif %}
  </div>
</section>

<script>
(function() {
  const form = document.getElementById('kb-search-form');
  const input = document.getElementById('kb-query');
  const answerCard = document.getElementById('kb-answer-card');
  const answerText = document.getElementById('kb-answer-text');
  const answerSources = document.getElementById('kb-answer-sources');
  const answerCta = document.getElementById('kb-answer-cta');
  const articleUrlTemplate = '{{ url_for('kb_article', article_id=0) }}';
  const fallback = {{ fallback_answer|tojson }};

  if (!form) {
    return;
  }

  form.addEventListener('submit', async function(event) {
    event.preventDefault();
    const query = (input.value || '').trim();
    if (!query) {
      input.focus();
      return;
    }

    answerCard.classList.remove('d-none');
    answerText.textContent = 'Thinking…';
    answerSources.textContent = '';
    answerCta.classList.add('d-none');

    try {
      const response = await fetch('{{ url_for('kb_ask') }}', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Requested-With': 'XMLHttpRequest'
        },
        body: JSON.stringify({ query })
      });

      const payload = await response.json();
      const results = Array.isArray(payload.results) ? payload.results : [];

      answerText.textContent = (payload.answer || '').trim() || fallback;
      answerSources.textContent = '';

      if (!response.ok || !results.length) {
        answerCta.classList.remove('d-none');
      } else {
        answerCta.classList.add('d-none');
      }

      if (results.length) {
        const label = document.createElement('span');
        label.textContent = 'Based on: ';
        answerSources.appendChild(label);

        results.forEach(function(result, index) {
          if (!result || typeof result.id === 'undefined') {
            return;
          }
          const link = document.createElement('a');
          const articleId = String(result.id);
          link.href = articleUrlTemplate.replace(/0$/, articleId);
          link.className = 'me-2';
          link.textContent = result.title || ('Article ' + articleId);
          answerSources.appendChild(link);
        });
      }
    } catch (error) {
      answerText.textContent = fallback;
      answerCta.classList.remove('d-none');
    }
  });
})();
</script>
{% endblock %}
"""


KB_ARTICLE_HTML = """
{% extends 'base.html' %}
{% block workspace_content %}
<div class="surface-card p-4 p-md-5">
  <div class="d-flex flex-column flex-md-row justify-content-between align-items-md-start gap-3 mb-4">
    <div>
      <span class="badge-chip badge-open text-uppercase small"><i class="bi bi-life-preserver"></i> Knowledge Base</span>
      <h1 class="h3 fw-semibold mt-3 mb-2">{{ article.title }}</h1>
      <p class="text-secondary mb-0">{{ article.summary }}</p>
    </div>
    <a class="btn btn-outline-dark" href="{{ url_for('kb_page') }}"><i class="bi bi-arrow-left"></i> Back to Knowledge Base</a>
  </div>

  {% if article.tags %}
  <div class="d-flex flex-wrap gap-2 mb-4">
    {% for tag in article.tags.split(',') if tag.strip() %}
    <span class="badge bg-light text-dark border">{{ tag.strip() }}</span>
    {% endfor %}
  </div>
  {% endif %}

  <div class="mb-5">
    <h2 class="h6 text-uppercase text-secondary fw-semibold">Resolution</h2>
    <div class="mt-2 text-secondary">{{ article.body|replace('\n', '<br>')|safe }}</div>
  </div>

  <div class="d-flex flex-column flex-md-row align-items-md-center justify-content-between gap-3">
    <div class="text-secondary small">
      <span class="me-3"><i class="bi bi-hand-thumbs-up"></i> Helpful: {{ article.helpful_up or 0 }}</span>
      <span><i class="bi bi-hand-thumbs-down"></i> Not helpful: {{ article.helpful_down or 0 }}</span>
    </div>
    <form method="post" action="{{ url_for('kb_article_vote', article_id=article.id) }}" class="d-flex gap-2">
      <button class="btn btn-outline-success" type="submit" name="vote" value="up"><i class="bi bi-hand-thumbs-up"></i> Helpful</button>
      <button class="btn btn-outline-danger" type="submit" name="vote" value="down"><i class="bi bi-hand-thumbs-down"></i> Not Helpful</button>
    </form>
  </div>
</div>
{% endblock %}
"""


# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------

@app.route("/")
def home():
    return render_template_string(HOME_HTML)


@app.route("/kb")
@login_required
def kb_page():
    with app.app_context():
        init_db()
    db = get_db()
    admin = is_admin_user()
    rows = db.execute(
        """
        SELECT id, title, summary, body, tags, helpful_up, helpful_down
        FROM kb_articles
        ORDER BY helpful_up DESC, helpful_down ASC, datetime(created_at) DESC, id DESC
        """
    ).fetchall()
    articles = [dict(row) for row in rows]
    recent_queries: list[sqlite3.Row] = []
    if admin:
        recent_rows = db.execute(
            """
            SELECT query, user_email, CAST(created_at AS TEXT) AS created_at
            FROM kb_queries
            ORDER BY datetime(created_at) DESC, id DESC
            LIMIT 10
            """
        ).fetchall()
        recent_queries = [dict(row) for row in recent_rows]
    return render_template_string(
        KB_HTML,
        articles=articles,
        fallback_answer=_kb_unavailable_message(),
        recent_queries=recent_queries,
        format_ts=format_timestamp,
    )


@app.route("/kb/ask", methods=["POST"])
@login_required
def kb_ask():
    payload = request.get_json(silent=True) or {}
    query = (payload.get("query") or "").strip()
    if not query:
        return jsonify({"error": "Query is required."}), 400

    if not is_it_question(query):
        return jsonify(
            {
                "answer": "This assistant answers IT questions only. Please rephrase with an IT topic or create a ticket.",
                "results": [],
            }
        )

    with app.app_context():
        init_db()
    db = get_db()
    db.execute(
        "INSERT INTO kb_queries (user_email, query, created_at) VALUES (?, ?, ?)",
        (current_user_email(), query, now_ts()),
    )
    db.commit()
    rows = db.execute(
        """
        SELECT a.id, a.title, a.summary, a.body, a.tags, a.helpful_up, a.helpful_down, e.embedding_json
        FROM kb_articles AS a
        INNER JOIN kb_embeddings AS e ON e.article_id = a.id
        """
    ).fetchall()

    articles_with_vectors: list[tuple[sqlite3.Row, list[float]]] = []
    for row in rows:
        vector = _load_embedding(row["embedding_json"])
        if vector:
            articles_with_vectors.append((row, vector))

    if not articles_with_vectors:
        return jsonify({"answer": _kb_unavailable_message(), "results": []})

    try:
        query_embedding = embed_text(query)
    except OpenAIServiceError as exc:
        app.logger.warning("Embedding failed: %s", exc)
        return jsonify({"answer": _kb_unavailable_message(), "results": []})

    scored: list[tuple[float, sqlite3.Row]] = []
    for row, vector in articles_with_vectors:
        score = cosine_similarity(query_embedding, vector)
        if score <= 0:
            continue
        scored.append((score, row))

    if not scored:
        return jsonify({"answer": _kb_unavailable_message(), "results": []})

    scored.sort(key=lambda item: item[0], reverse=True)
    relevant = [item for item in scored if item[0] >= KB_RELEVANCE_THRESHOLD][:KB_MAX_CONTEXT_ARTICLES]

    if not relevant:
        return jsonify({"answer": _kb_unavailable_message(), "results": []})

    context_parts: list[str] = []
    results_payload: list[dict[str, object]] = []
    for score, row in relevant:
        snippet = _truncate_body(row["body"])
        context_parts.append(
            f"Title: {row['title']}\nSummary: {row['summary']}\nDetails: {snippet}"
        )
        results_payload.append(
            {
                "id": row["id"],
                "title": row["title"],
                "summary": row["summary"],
                "score": round(float(score), 4),
            }
        )

    context = "\n\n".join(context_parts)
    try:
        answer = generate_answer(query, context)
    except OpenAIServiceError as exc:
        app.logger.warning("Answer generation failed: %s", exc)
        answer = _kb_unavailable_message()

    if not answer:
        answer = _kb_unavailable_message()

    return jsonify({"answer": answer, "results": results_payload})


@app.route("/feedbacks")
@login_required
def feedback_analytics():
    with app.app_context():
        init_db()
    db = get_db()
    admin = is_admin_user()

    base_conditions: list[str] = []
    base_params: list[str] = []

    if not admin:
        email = current_user_email()
        if email:
            base_conditions.append("LOWER(t.requester_email) = ?")
            base_params.append(email)
        else:
            star_counts_empty = {star: 0 for star in range(1, 6)}
            return render_template_string(
                FEEDBACK_ANALYTICS_HTML,
                entries=[],
                avg_rating="—",
                total_feedbacks=0,
                star_counts=star_counts_empty,
                requester_options=[],
                branches=BRANCHES,
                categories=CATEGORIES,
                selected_requester="",
                selected_branch="",
                selected_category="",
                filters_applied=[],
                format_ts=format_timestamp,
            )

    requester_conditions = [
        "t.requester_email IS NOT NULL",
        "TRIM(t.requester_email) != ''",
        *base_conditions,
    ]
    requester_sql = "SELECT DISTINCT t.requester_email FROM tickets t"
    if requester_conditions:
        requester_sql += " WHERE " + " AND ".join(requester_conditions)
    requester_sql += " ORDER BY LOWER(t.requester_email)"
    requester_rows = db.execute(requester_sql, base_params).fetchall()
    requester_options = [row[0] for row in requester_rows if row[0]]

    requester_filter = (request.args.get("requester_email") or "").strip()
    branch_filter = (request.args.get("branch") or "").strip()
    category_filter = (request.args.get("category") or "").strip()

    selected_requester = ""
    if requester_filter:
        lower_lookup = requester_filter.lower()
        for option in requester_options:
            if option and option.lower() == lower_lookup:
                selected_requester = option
                break

    selected_branch = branch_filter if branch_filter in BRANCHES else ""
    selected_category = category_filter if category_filter in CATEGORIES else ""

    conditions = list(base_conditions)
    params = list(base_params)
    filters_applied: list[tuple[str, str]] = []

    if selected_requester:
        conditions.append("LOWER(t.requester_email) = LOWER(?)")
        params.append(selected_requester)
        filters_applied.append(("Requester", selected_requester))

    if selected_branch:
        conditions.append("t.branch = ?")
        params.append(selected_branch)
        filters_applied.append(("Branch", selected_branch))

    if selected_category:
        conditions.append("t.category = ?")
        params.append(selected_category)
        filters_applied.append(("Category", selected_category))

    where_clause = ""
    if conditions:
        where_clause = " WHERE " + " AND ".join(conditions)

    stats_row = db.execute(
        f"""
        SELECT
            COUNT(*) AS total_feedbacks,
            AVG(tf.rating) AS avg_rating,
            SUM(CASE WHEN tf.rating = 5 THEN 1 ELSE 0 END) AS star_5,
            SUM(CASE WHEN tf.rating = 4 THEN 1 ELSE 0 END) AS star_4,
            SUM(CASE WHEN tf.rating = 3 THEN 1 ELSE 0 END) AS star_3,
            SUM(CASE WHEN tf.rating = 2 THEN 1 ELSE 0 END) AS star_2,
            SUM(CASE WHEN tf.rating = 1 THEN 1 ELSE 0 END) AS star_1
        FROM ticket_feedback tf
        JOIN tickets t ON t.id = tf.ticket_id
        {where_clause}
        """,
        params,
    ).fetchone()

    total_feedbacks = int(stats_row["total_feedbacks"] or 0) if stats_row else 0
    avg_value = stats_row["avg_rating"] if stats_row else None
    avg_rating = f"{avg_value:.1f}" if avg_value is not None else "0.0"
    star_counts = {
        5: int(stats_row["star_5"] or 0) if stats_row else 0,
        4: int(stats_row["star_4"] or 0) if stats_row else 0,
        3: int(stats_row["star_3"] or 0) if stats_row else 0,
        2: int(stats_row["star_2"] or 0) if stats_row else 0,
        1: int(stats_row["star_1"] or 0) if stats_row else 0,
    }

    entries_rows = db.execute(
        f"""
        SELECT tf.ticket_id, tf.rating, tf.comments, tf.submitted_by,
               CAST(tf.submitted_at AS TEXT) AS submitted_at,
               t.title, t.branch, t.category
        FROM ticket_feedback tf
        JOIN tickets t ON t.id = tf.ticket_id
        {where_clause}
        ORDER BY datetime(tf.submitted_at) DESC, tf.id DESC
        LIMIT 20
        """,
        params,
    ).fetchall()
    entries = [dict(row) for row in entries_rows]

    return render_template_string(
        FEEDBACK_ANALYTICS_HTML,
        entries=entries,
        avg_rating=avg_rating,
        total_feedbacks=total_feedbacks,
        star_counts=star_counts,
        requester_options=requester_options,
        branches=BRANCHES,
        categories=CATEGORIES,
        selected_requester=selected_requester,
        selected_branch=selected_branch,
        selected_category=selected_category,
        filters_applied=filters_applied,
        format_ts=format_timestamp,
    )


@app.route("/kb/article/<int:article_id>")
@login_required
def kb_article(article_id: int):
    with app.app_context():
        init_db()
    db = get_db()
    row = db.execute(
        """
        SELECT id, title, summary, body, tags, helpful_up, helpful_down
        FROM kb_articles
        WHERE id = ?
        """,
        (article_id,),
    ).fetchone()
    if not row:
        flash("Knowledge base article not found.")
        return redirect(url_for("kb_page"))
    return render_template_string(
        KB_ARTICLE_HTML,
        article=dict(row),
    )


@app.route("/kb/article/<int:article_id>/vote", methods=["POST"])
@login_required
def kb_article_vote(article_id: int):
    vote = request.form.get("vote")
    if vote not in {"up", "down"}:
        flash("Select whether the article was helpful or not.")
        return redirect(url_for("kb_article", article_id=article_id))

    with app.app_context():
        init_db()
    db = get_db()
    column = "helpful_up" if vote == "up" else "helpful_down"
    result = db.execute(
        f"UPDATE kb_articles SET {column} = {column} + 1 WHERE id = ?",
        (article_id,),
    )
    if result.rowcount == 0:
        flash("Knowledge base article not found.")
        return redirect(url_for("kb_page"))
    db.commit()
    flash("Thanks for letting us know!")
    return redirect(url_for("kb_article", article_id=article_id))

@app.route("/tickets")
@login_required
def tickets():
    if not DATABASE_URL:
        with app.app_context():
            init_db()

    admin = is_admin_user()

    status_filter = (request.args.get("status") or "").strip()
    priority_filter = (request.args.get("priority") or "").strip()
    branch_filter = (request.args.get("branch") or "").strip()
    category_filter = (request.args.get("category") or "").strip()

    filters_applied: list[tuple[str, str]] = []

    selected_status = status_filter if status_filter in STATUSES else ""
    selected_priority = priority_filter if priority_filter in PRIORITIES else ""
    selected_branch = branch_filter if branch_filter in BRANCHES else ""
    selected_category = category_filter if category_filter in CATEGORIES else ""

    session = get_session()

    try:
        q = session.query(Ticket)

        if selected_status:
            q = q.filter(Ticket.status == selected_status)
            filters_applied.append(("Status", selected_status))

        if selected_priority:
            q = q.filter(Ticket.priority == selected_priority)
            filters_applied.append(("Priority", selected_priority))

        if selected_branch:
            q = q.filter(Ticket.branch == selected_branch)
            filters_applied.append(("Branch", selected_branch))

        if selected_category:
            q = q.filter(Ticket.category == selected_category)
            filters_applied.append(("Category", selected_category))

        recent_feedback: list[dict[str, object]] = []

        if not admin:
            email = current_user_email()
            if email:
                q = q.filter(func.lower(Ticket.requester_email) == email)
            else:
                return render_template_string(
                    DASHBOARD_HTML,
                    tickets=[],
                    statuses=STATUSES,
                    priorities=PRIORITIES,
                    admin=admin,
                    stats={"total": 0, "open": 0, "completed": 0},
                    category_stats=[],
                    format_ts=format_timestamp,
                    branches=BRANCHES,
                    status_badges=STATUS_BADGES,
                    priority_badges=PRIORITY_BADGES,
                    selected_status="",
                    selected_priority="",
                    selected_branch="",
                    selected_category="",
                    categories=CATEGORIES,
                    filters_applied=[],
                    recent_feedback=recent_feedback,
                )
        else:
            feedback_rows = (
                session.query(TicketFeedback, Ticket)
                .join(Ticket, TicketFeedback.ticket)
                .order_by(TicketFeedback.submitted_at.desc())
                .limit(10)
                .all()
            )
            for feedback, ticket in feedback_rows:
                recent_feedback.append(
                    {
                        "ticket_id": ticket.id,
                        "title": ticket.title,
                        "rating": feedback.rating,
                        "comments": feedback.comments,
                        "submitted_by": feedback.submitted_by,
                        "submitted_at": feedback.submitted_at,
                        "branch": ticket.branch,
                        "category": ticket.category,
                    }
                )

        q = q.order_by(Ticket.created_at.desc(), Ticket.id.desc())

        ticket_rows = q.all()

        tickets = [
            {
                "id": row.id,
                "title": row.title,
                "requester_name": row.requester_name,
                "requester_email": row.requester_email,
                "branch": row.branch,
                "priority": row.priority,
                "category": row.category,
                "assignee": row.assignee,
                "status": row.status,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "completed_at": row.completed_at,
            }
            for row in ticket_rows
        ]

        total = len(tickets)
        completed = sum(1 for row in tickets if row.get("status") in COMPLETED_STATUSES)
        open_count = total - completed

        category_counter: Counter[str] = Counter()
        for row in tickets:
            category = (row.get("category") or "Uncategorized").strip() or "Uncategorized"
            category_counter[category] += 1

        category_stats = [
            {"category": name, "count": count}
            for name, count in category_counter.most_common()
        ]

        stats = {"total": total, "open": open_count, "completed": completed}

        return render_template_string(
            DASHBOARD_HTML,
            tickets=tickets,
            statuses=STATUSES,
            priorities=PRIORITIES,
            admin=admin,
            stats=stats,
            category_stats=category_stats,
            format_ts=format_timestamp,
            branches=BRANCHES,
            status_badges=STATUS_BADGES,
            priority_badges=PRIORITY_BADGES,
            selected_status=selected_status,
            selected_priority=selected_priority,
            selected_branch=selected_branch,
            selected_category=selected_category,
            categories=CATEGORIES,
            filters_applied=filters_applied,
            recent_feedback=recent_feedback,
        )
    finally:
        session.close()


@app.route("/new", methods=["GET", "POST"])
@login_required
def new_ticket():
    init_db()
    if request.method == "POST":
        data = {k: (request.form.get(k) or "").strip() for k in [
            "title", "description", "requester_name", "requester_email", "branch", "priority", "category"
        ]}

        if not data["title"] or not data["description"]:
            flash("Title and Description are required.")
            return redirect(url_for("new_ticket"))

        attachments_to_save: list[dict[str, object]] = []
        total_size = 0
        for upload in request.files.getlist("attachments"):
            if not upload or not upload.filename:
                continue
            file_data = upload.read()
            if not file_data:
                continue
            total_size += len(file_data)
            if total_size > MAX_ATTACHMENT_TOTAL_BYTES:
                flash(
                    "Attachments exceed the total upload limit of "
                    f"{format_file_size(MAX_ATTACHMENT_TOTAL_BYTES)}."
                )
                return redirect(url_for("new_ticket"))
            filename = secure_filename(upload.filename) or f"attachment-{len(attachments_to_save) + 1}"
            attachments_to_save.append(
                {
                    "filename": filename,
                    "content_type": upload.mimetype,
                    "data": file_data,
                }
            )

        session: Session = get_session()
        ts = now_ts()
        ticket = Ticket(
            title=data["title"],
            description=data["description"],
            requester_name=data["requester_name"],
            requester_email=data["requester_email"],
            branch=data["branch"],
            priority=data["priority"] or "Medium",
            category=data["category"],
            assignee=ASSIGNEE_DEFAULT,
            status="Open",
            created_at=ts,
            updated_at=ts,
            completed_at=None,
            feedback_token=generate_feedback_token(),
        )
        session.add(ticket)
        session.flush()
        ticket_id = ticket.id

        if attachments_to_save:
            uploaded_ts = now_ts()
            for item in attachments_to_save:
                session.add(
                    Attachment(
                        ticket_id=ticket_id,
                        filename=item["filename"],
                        content_type=item["content_type"],
                        data=item["data"],
                        uploaded_at=uploaded_ts,
                    )
                )

        session.commit()

        # --- Email notifications (uses session['access_token']) ---
        subject_admin = f"A new ticket has been submitted by {data['requester_name'] or 'Unknown User'}"
        description_html = data["description"].replace("\n", "<br>")
        body_admin = f"""
        <p><strong>A new ticket has been submitted to Seegars IT.</strong></p>
        <p><strong>Submitted by:</strong> {data['requester_name']} &lt;{data['requester_email']}&gt;</p>
        <p><strong>Priority:</strong> {data['priority']}<br>
        <strong>Branch:</strong> {data['branch']}<br>
        <strong>Category:</strong> {data['category']}</p>
        <p><strong>Issue Description:</strong><br>{description_html}</p>
        <p>Thank you,<br><br>Brad Wells<br>IT Manager</p>
        """
        admin_recipients = sorted(ADMIN_EMAILS) or ["brad@seegarsfence.com"]
        send_email(admin_recipients, subject_admin, body_admin)

        if data["requester_email"]:
            ticket_link = ticket_detail_link(ticket_id)
            subject_user = "Your ticket to Seegars IT has been received"
            body_user = f"""
            <p>Hi {data['requester_name'] or ''},</p>
            <p>Thank you for contacting <strong>Seegars IT</strong>. Your ticket has been received and will be reviewed soon.</p>
            <p>It will be prioritized based on the selection you made, and you’ll receive a response as soon as possible.</p>
            <p><strong>Summary:</strong><br>
            Priority: {data['priority']}<br>
            Branch: {data['branch']}<br>
            Category: {data['category']}</p>
            <p><strong>Issue Description:</strong><br>{description_html}</p>
            <p><a href="{ticket_link}">View your ticket</a> any time to share additional details or check progress.</p>
            <p><em>We appreciate your patience — our goal is to keep your tech running smoothly!</em></p>
            <p>Thank you,<br><br>Brad Wells<br>IT Manager</p>
            """
            send_email(data["requester_email"], subject_user, body_user)

        flash("Ticket created successfully!")
        return redirect(url_for("tickets"))

    # GET request: show the form
    return render_template_string(
        NEW_HTML,
        priorities=PRIORITIES,
        branches=BRANCHES,
        categories=CATEGORIES,
        attachment_limit=format_file_size(MAX_ATTACHMENT_TOTAL_BYTES),
    )


@app.route("/ticket/<int:ticket_id>")
@login_required
def ticket_detail(ticket_id: int):
    with app.app_context():
        init_db()
    session: Session = get_session()

    ticket = (
        session.query(Ticket)
        .options(
            joinedload(Ticket.comments),
            joinedload(Ticket.attachments),
        )
        .filter(Ticket.id == ticket_id)
        .one_or_none()
    )
    if not ticket:
        flash("Ticket not found.")
        return redirect(url_for("tickets"))

    def _normalize_ts(value):
        if isinstance(value, datetime):
            dt = value
        else:
            text = str(value) if value is not None else ""
            if not text:
                return datetime.min.replace(tzinfo=timezone.utc)
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(text)
            except Exception:
                return datetime.min.replace(tzinfo=timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    admin = is_admin_user()
    comments = [
        {
            "id": comment.id,
            "ticket_id": comment.ticket_id,
            "author": comment.author,
            "body": comment.body,
            "created_at": comment.created_at,
        }
        for comment in sorted(
            ticket.comments,
            key=lambda c: (_normalize_ts(c.created_at), c.id or 0),
        )
    ]
    attachments = []
    for attachment in sorted(
        ticket.attachments,
        key=lambda a: (_normalize_ts(a.uploaded_at), a.id or 0),
    ):
        data = attachment.data or b""
        size = len(data)
        attachments.append(
            {
                "id": attachment.id,
                "filename": attachment.filename,
                "content_type": attachment.content_type,
                "size": size,
                "size_label": format_file_size(size),
                "uploaded_at": attachment.uploaded_at,
            }
        )

    feedback_entries: list[dict[str, object]] = []
    if admin:
        feedback_entries = [
            {
                "rating": feedback.rating,
                "comments": feedback.comments,
                "submitted_by": feedback.submitted_by,
                "submitted_at": feedback.submitted_at,
            }
            for feedback in (
                session.query(TicketFeedback)
                .filter(TicketFeedback.ticket_id == ticket_id)
                .order_by(desc(TicketFeedback.submitted_at), TicketFeedback.id.desc())
                .all()
            )
        ]

    ticket_data = {
        "id": ticket.id,
        "title": ticket.title,
        "description": ticket.description,
        "requester_name": ticket.requester_name,
        "requester_email": ticket.requester_email,
        "branch": ticket.branch,
        "priority": ticket.priority,
        "category": ticket.category,
        "assignee": ticket.assignee,
        "status": ticket.status,
        "created_at": ticket.created_at,
        "updated_at": ticket.updated_at,
        "completed_at": ticket.completed_at,
    }
    return render_template_string(
        DETAIL_HTML,
        t=ticket_data,
        comments=comments,
        attachments=attachments,
        statuses=STATUSES,
        admin=admin,
        format_ts=format_timestamp,
        status_badges=STATUS_BADGES,
        priority_badges=PRIORITY_BADGES,
        feedback_entries=feedback_entries,
    )


@app.route("/ticket/<int:ticket_id>/attachment/<int:attachment_id>")
@login_required
def download_attachment(ticket_id: int, attachment_id: int):
    with app.app_context():
        init_db()
    session: Session = get_session()
    attachment = (
        session.query(Attachment)
            .filter(
                Attachment.id == attachment_id,
                Attachment.ticket_id == ticket_id,
            )
            .one_or_none()
    )
    if not attachment:
        abort(404)
    return send_file(
        io.BytesIO(attachment.data or b""),
        download_name=attachment.filename,
        mimetype=attachment.content_type or "application/octet-stream",
        as_attachment=True,
    )


@app.route("/ticket/<int:ticket_id>/comment", methods=["POST"])
@login_required
def add_comment(ticket_id: int):
    with app.app_context():
        init_db()
    body = (request.form.get("body") or "").strip()
    author = (request.form.get("author") or "").strip()
    if not body:
        flash("Comment cannot be empty.")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))
    session: Session = get_session()
    ticket = session.query(Ticket).filter(Ticket.id == ticket_id).one_or_none()
    if not ticket:
        flash("Ticket not found.")
        return redirect(url_for("tickets"))

    ts = now_ts()
    comment = Comment(
        ticket_id=ticket_id,
        author=author,
        body=body,
        created_at=ts,
    )
    session.add(comment)
    ticket.updated_at = ts
    session.commit()

    if is_admin_user():
        ticket_row = {
            "title": ticket.title,
            "requester_name": ticket.requester_name,
            "requester_email": ticket.requester_email,
            "feedback_token": ticket.feedback_token,
        }
        if ticket_row["requester_email"]:
            requester_name = (ticket_row["requester_name"] or "there").strip() or "there"
            ticket_title = ticket_row["title"] or f"Ticket #{ticket_id}"
            comment_html = str(escape(body)).replace("\n", "<br>")
            author_label = (author or "Seegars IT").strip() or "Seegars IT"
            ticket_link = ticket_detail_link(ticket_id)
            subject = f"Ticket update: {ticket_title}"
            body_html = f"""
            <p>Hi {escape(requester_name)},</p>
            <p>{escape(author_label)} added a new update to your ticket <strong>{escape(ticket_title)}</strong>.</p>
            <div style=\"border-left:4px solid #008752;padding-left:12px;margin:16px 0;\">
              <p style=\"margin:0;\">{comment_html}</p>
            </div>
            <p><a href="{ticket_link}">Open your ticket</a> to review the update or add more details.</p>
            <p>Thank you,<br><br>Brad Wells<br>IT Manager</p>
            """
            send_ticket_notification(ticket_row, subject, body_html)
    flash("Comment added.")
    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


@app.route("/ticket/<int:ticket_id>/status", methods=["POST"])
@login_required
def update_status(ticket_id: int):
    if not is_admin_user():
        abort(403)
    with app.app_context():
        init_db()
    new_status = (request.form.get("status") or "Open").strip()
    if new_status not in STATUSES:
        new_status = "Open"
    session: Session = get_session()
    ticket = session.get(Ticket, ticket_id)
    if not ticket:
        flash("Ticket not found.")
        return redirect(url_for("tickets"))
    previous_status = ticket.status
    ts = now_ts()
    ticket.status = new_status
    ticket.updated_at = ts
    ticket.completed_at = ts if new_status in COMPLETED_STATUSES else None
    if not ticket.feedback_token:
        ticket.feedback_token = generate_feedback_token()
    session.commit()
    ticket_row = {
        "title": ticket.title,
        "requester_name": ticket.requester_name,
        "requester_email": ticket.requester_email,
        "feedback_token": ticket.feedback_token,
    }
    if (
        ticket_row["requester_email"]
        and previous_status != new_status
    ):
        ticket_title = ticket_row["title"] or f"Ticket #{ticket_id}"
        requester_name = (ticket_row["requester_name"] or "there").strip() or "there"
        ticket_link = ticket_detail_link(ticket_id)
        if new_status in COMPLETED_STATUSES and previous_status not in COMPLETED_STATUSES:
            feedback_url = ticket_feedback_link(ticket_id, ticket.feedback_token or "")
            subject = f"Ticket completed: {ticket_title}"
            body_html = f"""
            <p>Hi {escape(requester_name)},</p>
            <p>Your ticket <strong>{escape(ticket_title)}</strong> has been marked <strong>{escape(new_status)}</strong>.</p>
            <p>You can review the final details on the <a href="{ticket_link}">ticket page</a>.</p>
            <p>We value your perspective. Please take a moment to <a href="{feedback_url}">share feedback on this experience</a>.</p>
            <p>Thank you,<br><br>Brad Wells<br>IT Manager</p>
            """
        else:
            subject = f"Ticket status update: {ticket_title}"
            body_html = f"""
            <p>Hi {escape(requester_name)},</p>
            <p>Your ticket <strong>{escape(ticket_title)}</strong> is now marked <strong>{escape(new_status)}</strong>.</p>
            <p><a href="{ticket_link}">Open your ticket</a> to review progress or add more information.</p>
            <p>Thank you,<br><br>Brad Wells<br>IT Manager</p>
            """
        send_ticket_notification(ticket_row, subject, body_html)
    flash("Status updated.")
    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


@app.route("/ticket/<int:ticket_id>/assignee", methods=["POST"])
@login_required
def update_assignee(ticket_id: int):
    if not is_admin_user():
        abort(403)
    with app.app_context():
        init_db()
    assignee = (request.form.get("assignee") or "").strip()
    session: Session = get_session()
    ticket = session.get(Ticket, ticket_id)
    if not ticket:
        flash("Ticket not found.")
        return redirect(url_for("tickets"))
    previous_assignee = (ticket.assignee or "").strip()
    ticket.assignee = assignee
    ticket.updated_at = now_ts()
    session.commit()
    ticket_row = {
        "title": ticket.title,
        "requester_name": ticket.requester_name,
        "requester_email": ticket.requester_email,
    }
    if ticket_row["requester_email"] and assignee != previous_assignee:
        ticket_title = ticket_row["title"] or f"Ticket #{ticket_id}"
        requester_name = (ticket_row["requester_name"] or "there").strip() or "there"
        ticket_link = ticket_detail_link(ticket_id)
        assignee_label = assignee or "Unassigned"
        subject = f"Ticket assignment update: {ticket_title}"
        body_html = f"""
        <p>Hi {escape(requester_name)},</p>
        <p>Your ticket <strong>{escape(ticket_title)}</strong> has been assigned to <strong>{escape(assignee_label)}</strong>.</p>
        <p><a href="{ticket_link}">Open your ticket</a> if you have more information to share.</p>
        <p>Thank you,<br><br>Brad Wells<br>IT Manager</p>
        """
        send_ticket_notification(ticket_row, subject, body_html)
    flash("Assignee updated.")
    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


@app.route("/ticket/<int:ticket_id>/feedback", methods=["GET", "POST"])
def ticket_feedback(ticket_id: int):
    with app.app_context():
        init_db()
    db = get_db()
    token = (request.values.get("token") or "").strip()
    ticket_row = db.execute(
        """
        SELECT id, title, requester_name, feedback_token
        FROM tickets
        WHERE id = ?
        """,
        (ticket_id,),
    ).fetchone()
    if not ticket_row:
        return (
            render_template_string(
                FEEDBACK_HTML,
                ticket=None,
                show_form=False,
                submitted=False,
                error="We couldn't find that ticket.",
                rating_choices=FEEDBACK_RATING_OPTIONS,
                rating="",
                comments="",
                submitted_by="",
                token=token,
                ticket_link=None,
            ),
            404,
        )

    expected_token = ticket_row["feedback_token"] or ensure_ticket_feedback_token(ticket_id, db=db)
    ticket_link = ticket_detail_link(ticket_id)
    if not token or token != expected_token:
        return (
            render_template_string(
                FEEDBACK_HTML,
                ticket=dict(ticket_row),
                show_form=False,
                submitted=False,
                error="This feedback link is no longer valid.",
                rating_choices=FEEDBACK_RATING_OPTIONS,
                rating="",
                comments="",
                submitted_by="",
                token="",
                ticket_link=ticket_link,
            ),
            403,
        )

    if request.method == "POST":
        rating_raw = (request.form.get("rating") or "").strip()
        comments = (request.form.get("comments") or "").strip()
        submitted_by = (request.form.get("submitted_by") or "").strip()
        rating = None
        if rating_raw:
            try:
                rating = int(rating_raw)
            except ValueError:
                rating = None
        if rating is not None and rating not in {1, 2, 3, 4, 5}:
            rating = None
        if not rating and not comments:
            return render_template_string(
                FEEDBACK_HTML,
                ticket=dict(ticket_row),
                show_form=True,
                submitted=False,
                error="Please select a rating or leave a comment.",
                rating_choices=FEEDBACK_RATING_OPTIONS,
                rating=rating_raw,
                comments=comments,
                submitted_by=submitted_by,
                token=token,
                ticket_link=ticket_link,
            )
        db.execute(
            """
            INSERT INTO ticket_feedback (ticket_id, rating, comments, submitted_by, submitted_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (ticket_id, rating, comments or None, submitted_by or None, now_ts()),
        )
        db.commit()
        return render_template_string(
            FEEDBACK_HTML,
            ticket=dict(ticket_row),
            show_form=False,
            submitted=True,
            error=None,
            rating_choices=FEEDBACK_RATING_OPTIONS,
            rating="",
            comments="",
            submitted_by="",
            token=token,
            ticket_link=ticket_link,
        )

    return render_template_string(
        FEEDBACK_HTML,
        ticket=dict(ticket_row),
        show_form=True,
        submitted=False,
        error=None,
        rating_choices=FEEDBACK_RATING_OPTIONS,
        rating="",
        comments="",
        submitted_by="",
        token=token,
        ticket_link=ticket_link,
    )


# --------------------------------------------------------------------------------------
# Microsoft 365 sign-in routes
# --------------------------------------------------------------------------------------

@app.route("/login")
def login():
    # If env vars not set, show a friendly error
    if not (CLIENT_ID and CLIENT_SECRET and AUTHORITY and REDIRECT_URI):
        flash("Microsoft login not configured. Set env vars on Render.")
        return redirect(url_for("tickets"))  # changed here
    state = str(uuid.uuid4())
    session["state"] = state
    auth_url = msal_app().get_authorization_request_url(
        scopes=SCOPE,
        redirect_uri=REDIRECT_URI,
        state=state,
        response_mode="query",
        prompt="select_account",
    )
    return redirect(auth_url)


@app.route("/auth/callback", methods=["GET", "POST"])
def auth_callback():
    # state & code can arrive via GET (args) or POST (form)
    state = request.values.get("state")
    if state != session.get("state"):
        return ("State mismatch", 400)

    code = request.values.get("code")  # works for both GET and POST
    if not code:
        flash("No authorization code returned.")
        return redirect(url_for("tickets"))  # changed here

    result = msal_app().acquire_token_by_authorization_code(
        code,
        scopes=SCOPE,
        redirect_uri=REDIRECT_URI,
    )
    session["access_token"] = result.get("access_token")

    if "id_token_claims" not in result:
        flash("Login failed.")
        return redirect(url_for("tickets"))  # changed here too

    claims = result["id_token_claims"]
    session["user"] = {
        "name": claims.get("name") or claims.get("preferred_username"),
        "email": claims.get("preferred_username"),
        "oid": claims.get("oid"),
    }
    flash(f"Signed in as {session['user']['email']}")
    return redirect(url_for("tickets"))  # and this one too


@app.route("/logout")
def logout():
    session.clear()
    flash("Signed out.")
    return redirect(url_for("home"))

# --------------------------------------------------------------------------------------
# Jinja loader (since we keep templates inline in this single file)
# --------------------------------------------------------------------------------------
app.jinja_loader = DictLoader({
    "base.html": BASE_HTML,
    "home.html": HOME_HTML,   # ← add this line
    "kb.html": KB_HTML,
    "kb_article.html": KB_ARTICLE_HTML,
    "dashboard.html": DASHBOARD_HTML,
    "feedback_analytics.html": FEEDBACK_ANALYTICS_HTML,
    "new.html": NEW_HTML,
    "detail.html": DETAIL_HTML,
    "feedback.html": FEEDBACK_HTML,
})

# --------------------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------------------
if __name__ == "__main__":
    # Ensure we are inside an application context before using `g`
    with app.app_context():
        init_db()
    # Render provides a PORT env var; bind to 0.0.0.0 for external access
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
