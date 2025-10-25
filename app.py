# test change from Codex
from __future__ import annotations
import io
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional
from functools import wraps

from collections import Counter

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template_string,
    request,
    send_file,
    session,
    url_for,
)
from markupsafe import escape
from jinja2 import DictLoader
from msal import ConfidentialClientApplication
import uuid
import requests
from werkzeug.utils import secure_filename

def send_email(to_addrs, subject, html_body):
    """Send an email via Microsoft Graph using the current user's access token."""
    token = session.get("access_token")
    if not token:
        # Not signed in (or token expired); skip quietly for now
        return False

    if isinstance(to_addrs, str):
        to_addrs = [to_addrs]

    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to_addrs],
        }
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(
            "https://graph.microsoft.com/v1.0/me/sendMail",
            headers=headers,
            json=payload,
            timeout=10,
        )
        return resp.status_code in (200, 202)
    except Exception:
        return False

# --------------------------------------------------------------------------------------
# Flask app config
# --------------------------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET", "dev-secret")
DB_PATH = os.environ.get("TICKETS_DB", "app.db")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB overall request cap

# Microsoft Entra (Azure AD / M365) app details come from environment variables on Render
CLIENT_ID = os.getenv("MICROSOFT_CLIENT_ID")
TENANT_ID = os.getenv("MICROSOFT_TENANT_ID")
CLIENT_SECRET = os.getenv("MICROSOFT_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")  # e.g., https://seegarsit.onrender.com/auth/callback
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}" if TENANT_ID else None
SCOPE = ["User.Read", "Mail.Send"]


# --------------------------------------------------------------------------------------
# DB helpers
# --------------------------------------------------------------------------------------

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

        CREATE INDEX IF NOT EXISTS idx_ticket_feedback_ticket ON ticket_feedback(ticket_id);

        CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
        CREATE INDEX IF NOT EXISTS idx_tickets_priority ON tickets(priority);
        CREATE INDEX IF NOT EXISTS idx_tickets_branch ON tickets(branch);
        CREATE INDEX IF NOT EXISTS idx_comments_ticket ON comments(ticket_id);
        CREATE INDEX IF NOT EXISTS idx_attachments_ticket ON attachments(ticket_id);
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

    raw = os.getenv("ADMIN_EMAILS", "brad@seegarsfence.com")
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
        <a class="nav-pill disabled" href="#" onclick="return false;"><i class="bi bi-life-preserver"></i>Knowledge Base</a>
      </div>
      <div>
        <div class="nav-section-title">Shortcuts</div>
        <div class="filter-pill"><i class="bi bi-funnel"></i>Saved Views</div>
        <div class="filter-pill"><i class="bi bi-lightning"></i>Automation Rules</div>
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
    <button type="button" class="btn btn-outline-dark d-flex align-items-center gap-2"><i class="bi bi-bookmark-star"></i>Save View</button>
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

<div class="row g-4 align-items-start">
  <div class="col-lg-4 col-xl-3">
    <div class="filter-panel">
      <div class="d-flex align-items-center justify-content-between mb-3">
        <h5 class="mb-0">Quick Filters</h5>
        <button type="button" class="btn btn-sm btn-outline-dark">Reset</button>
      </div>
      <form class="d-flex flex-column gap-3">
        <div>
          <label class="form-label small text-uppercase">Search tickets</label>
          <div class="input-group input-group-sm">
            <span class="input-group-text bg-white border-end-0"><i class="bi bi-search"></i></span>
            <input type="search" class="form-control border-start-0" placeholder="Search by title or #" disabled>
          </div>
        </div>
        <div>
          <label class="form-label small text-uppercase">Status</label>
          <div class="d-flex flex-wrap gap-2">
            {% for s in statuses %}
              <span class="filter-pill"><i class="bi bi-circle-fill" style="font-size:0.55rem;"></i>{{ s }}</span>
            {% endfor %}
          </div>
        </div>
        <div>
          <label class="form-label small text-uppercase">Priority</label>
          <div class="d-flex flex-wrap gap-2">
            {% for p in priorities %}
              <span class="filter-pill"><i class="bi bi-sliders"></i>{{ p }}</span>
            {% endfor %}
          </div>
        </div>
        {% if admin %}
        <div>
          <label class="form-label small text-uppercase">Branch</label>
          <div class="d-flex flex-wrap gap-2">
            {% for b in branches[:6] %}
              <span class="filter-pill"><i class="bi bi-geo"></i>{{ b }}</span>
            {% endfor %}
            {% if branches|length > 6 %}
              <span class="filter-pill"><i class="bi bi-three-dots"></i>More</span>
            {% endif %}
          </div>
        </div>
        {% endif %}
        <div class="text-secondary small">Interactive filtering coming soon — adjust presets above and save your favourite view.</div>
      </form>
    </div>
    {% if admin %}
    <div class="surface-card p-4 mt-4">
      <div class="d-flex align-items-center justify-content-between mb-2">
        <h5 class="mb-0">Recent Feedback</h5>
        <span class="badge-chip badge-complete"><i class="bi bi-chat-heart"></i>{{ feedback_entries|length }}</span>
      </div>
      <div class="d-flex flex-column gap-3">
        {% for fb in feedback_entries %}
        <div class="border rounded p-3">
          <div class="d-flex justify-content-between align-items-center mb-1">
            <strong class="small">{{ fb.ticket_title or ('Ticket #' ~ fb.ticket_id) }}</strong>
            {% if fb.rating %}<span class="text-warning small">{{ fb.rating }}/5</span>{% endif %}
          </div>
          <p class="mb-1 small">{{ fb.comments or 'No comments provided.' }}</p>
          <div class="text-secondary small">{{ format_ts(fb.submitted_at) }}</div>
        </div>
        {% else %}
        <p class="text-secondary small mb-0">Feedback will appear here after tickets close.</p>
        {% endfor %}
      </div>
    </div>
    {% endif %}
  </div>
  <div class="col-lg-8 col-xl-9">
    <div class="surface-card p-0 overflow-hidden">
      <div class="d-flex flex-wrap align-items-center justify-content-between gap-2 p-4 border-bottom border-light-subtle">
        <div class="d-flex align-items-center gap-3">
          <div class="badge-chip badge-open"><i class="bi bi-lightning"></i>{{ tickets|length }} Results</div>
          <span class="text-secondary small">Sorted by most recent updates</span>
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
            <input name="title" class="form-control" placeholder="Example: AccountMate won’t open" required>
          </div>
          <div>
            <label class="form-label fw-semibold">What’s happening?</label>
            <textarea name="description" class="form-control" rows="6" placeholder="Share clear details, steps, and any error messages." required></textarea>
          </div>
          <div class="row g-3">
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
            <select name="priority" class="form-select">
              {% for option in priorities %}
              <option value="{{ option }}" {% if option == 'Medium' %}selected{% endif %}>{{ option }}</option>
              {% endfor %}
            </select>
          </div>
          <div class="mb-3">
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
          <div class="mb-3">
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
      <button class="btn btn-primary d-flex align-items-center gap-2" type="submit"><i class="bi bi-send"></i>Submit ticket</button>
    </div>
  </form>
  <script>
    const attachmentInput = document.getElementById('attachments');
    const attachmentList = document.getElementById('attachment-list');
    if (attachmentInput && attachmentList) {
      attachmentInput.addEventListener('change', () => {
        attachmentList.innerHTML = '';
        if (!attachmentInput.files || attachmentInput.files.length === 0) {
          return;
        }
        Array.from(attachmentInput.files).forEach((file) => {
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
    }
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
        <a class="btn btn-outline-dark d-flex align-items-center gap-2" href="{{ url_for('tickets') }}"><i class="bi bi-eye"></i>Preview workspace</a>
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


# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------

@app.route("/")
def home():
    return render_template_string(HOME_HTML)

@app.route("/tickets")
@login_required
def tickets():
    with app.app_context():
        init_db()
    db = get_db()
    admin = is_admin_user()

    sql = (
        "SELECT id, title, requester_name, requester_email, branch, priority, category, "
        "assignee, status, CAST(created_at AS TEXT) AS created_at, "
        "CAST(updated_at AS TEXT) AS updated_at, CAST(completed_at AS TEXT) AS completed_at "
        "FROM tickets"
    )

    params: list[str] = []
    if not admin:
        email = current_user_email()
        if email:
            sql += " WHERE LOWER(requester_email) = ?"
            params.append(email)
        else:
            tickets = []
            stats = {"total": 0, "open": 0, "completed": 0}
            category_stats: list[dict[str, str | int]] = []
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
                feedback_entries=[],
            )

    sql += " ORDER BY datetime(created_at) DESC, id DESC"
    rows = db.execute(sql, params).fetchall()
    tickets = [dict(row) for row in rows]

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

    feedback_entries: list[dict[str, object]] = []
    if admin:
        feedback_rows = db.execute(
            """
            SELECT tf.ticket_id, tf.rating, tf.comments, tf.submitted_by,
                   CAST(tf.submitted_at AS TEXT) AS submitted_at,
                   t.title AS ticket_title
            FROM ticket_feedback tf
            JOIN tickets t ON t.id = tf.ticket_id
            ORDER BY datetime(tf.submitted_at) DESC, tf.id DESC
            LIMIT 10
            """,
        ).fetchall()
        feedback_entries = [dict(row) for row in feedback_rows]

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
        feedback_entries=feedback_entries,
    )


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

        ts = now_ts()
        feedback_token = generate_feedback_token()
        db = get_db()
        cur = db.execute(
            """
            INSERT INTO tickets (
                title, description, requester_name, requester_email, branch, priority,
                category, assignee, status, created_at, updated_at, completed_at, feedback_token
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Open', ?, ?, NULL, ?)
            """,
            (
                data["title"],
                data["description"],
                data["requester_name"],
                data["requester_email"],
                data["branch"],
                data["priority"],
                data["category"],
                ASSIGNEE_DEFAULT,
                ts,
                ts,
                feedback_token,
            )
        )
        ticket_id = cur.lastrowid
        if attachments_to_save:
            uploaded_ts = now_ts()
            for item in attachments_to_save:
                db.execute(
                    """
                    INSERT INTO attachments (ticket_id, filename, content_type, data, uploaded_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        ticket_id,
                        item["filename"],
                        item["content_type"],
                        item["data"],
                        uploaded_ts,
                    ),
                )
        db.commit()

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
        """
        send_email("brad@seegarsfence.com", subject_admin, body_admin)

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
    db = get_db()
    t = db.execute(
        """
        SELECT id, title, description, requester_name, requester_email, branch, priority,
               category, assignee, status,
               CAST(created_at AS TEXT) AS created_at,
               CAST(updated_at AS TEXT) AS updated_at,
               CAST(completed_at AS TEXT) AS completed_at
        FROM tickets WHERE id = ?
        """,
        (ticket_id,),
    ).fetchone()
    if not t:
        flash("Ticket not found.")
        return redirect(url_for("tickets"))
    admin = is_admin_user()
    comments = db.execute(
        """
        SELECT id, ticket_id, author, body, CAST(created_at AS TEXT) AS created_at
        FROM comments WHERE ticket_id = ? ORDER BY datetime(created_at) ASC
        """,
        (ticket_id,),
    ).fetchall()
    attachments_rows = db.execute(
        """
        SELECT id, filename, content_type, LENGTH(data) AS size, CAST(uploaded_at AS TEXT) AS uploaded_at
        FROM attachments
        WHERE ticket_id = ?
        ORDER BY datetime(uploaded_at) ASC, id ASC
        """,
        (ticket_id,),
    ).fetchall()
    attachments = [
        {
            "id": row["id"],
            "filename": row["filename"],
            "content_type": row["content_type"],
            "size": row["size"] or 0,
            "size_label": format_file_size(int(row["size"] or 0)),
            "uploaded_at": row["uploaded_at"],
        }
        for row in attachments_rows
    ]
    feedback_entries: list[dict[str, object]] = []
    if admin:
        feedback_rows = db.execute(
            """
            SELECT rating, comments, submitted_by, CAST(submitted_at AS TEXT) AS submitted_at
            FROM ticket_feedback
            WHERE ticket_id = ?
            ORDER BY datetime(submitted_at) DESC, id DESC
            """,
            (ticket_id,),
        ).fetchall()
        feedback_entries = [dict(row) for row in feedback_rows]
    return render_template_string(
        DETAIL_HTML,
        t=dict(t),
        comments=[dict(c) for c in comments],
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
    db = get_db()
    attachment = db.execute(
        """
        SELECT filename, content_type, data
        FROM attachments
        WHERE id = ? AND ticket_id = ?
        """,
        (attachment_id, ticket_id),
    ).fetchone()
    if not attachment:
        abort(404)
    return send_file(
        io.BytesIO(attachment["data"]),
        download_name=attachment["filename"],
        mimetype=attachment["content_type"] or "application/octet-stream",
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
    db = get_db()
    db.execute(
        "INSERT INTO comments (ticket_id, author, body, created_at) VALUES (?,?,?,?)",
        (ticket_id, author, body, now_ts()),
    )
    db.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (now_ts(), ticket_id))
    db.commit()
    if is_admin_user():
        ticket_row = get_ticket_contact(ticket_id, db=db)
        if ticket_row:
            requester_name = (ticket_row["requester_name"] or "there").strip() or "there"
            ticket_title = ticket_row["title"] or f"Ticket #{ticket_id}"
            comment_html = str(escape(body)).replace("\n", "<br>")
            author_label = (author or "Seegars IT").strip() or "Seegars IT"
            ticket_link = ticket_detail_link(ticket_id)
            subject = f"Ticket update: {ticket_title}"
            body_html = f"""
            <p>Hi {escape(requester_name)},</p>
            <p>{escape(author_label)} added a new update to your ticket <strong>{escape(ticket_title)}</strong>.</p>
            <div style="border-left:4px solid #008752;padding-left:12px;margin:16px 0;">
              <p style=\"margin:0;\">{comment_html}</p>
            </div>
            <p><a href="{ticket_link}">Open your ticket</a> to review the update or add more details.</p>
            <p>Thank you,<br>Seegars IT</p>
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
    status = (request.form.get("status") or "Open").strip()
    if status not in STATUSES:
        status = "Open"
    db = get_db()
    ticket_row = db.execute(
        """
        SELECT status, title, requester_name, requester_email, feedback_token
        FROM tickets
        WHERE id = ?
        """,
        (ticket_id,),
    ).fetchone()
    if not ticket_row:
        flash("Ticket not found.")
        return redirect(url_for("tickets"))
    previous_status = ticket_row["status"]
    ts = now_ts()
    completed_at = ts if status in COMPLETED_STATUSES else None
    feedback_token = ticket_row["feedback_token"] or generate_feedback_token()
    db.execute(
        """
        UPDATE tickets
        SET status = ?, updated_at = ?, completed_at = ?, feedback_token = COALESCE(feedback_token, ?)
        WHERE id = ?
        """,
        (status, ts, completed_at, feedback_token, ticket_id),
    )
    db.commit()
    if (
        ticket_row["requester_email"]
        and previous_status != status
    ):
        ticket_title = ticket_row["title"] or f"Ticket #{ticket_id}"
        requester_name = (ticket_row["requester_name"] or "there").strip() or "there"
        ticket_link = ticket_detail_link(ticket_id)
        if status in COMPLETED_STATUSES and previous_status not in COMPLETED_STATUSES:
            feedback_url = ticket_feedback_link(ticket_id, feedback_token)
            subject = f"Ticket completed: {ticket_title}"
            body_html = f"""
            <p>Hi {escape(requester_name)},</p>
            <p>Your ticket <strong>{escape(ticket_title)}</strong> has been marked <strong>{escape(status)}</strong>.</p>
            <p>You can review the final details on the <a href="{ticket_link}">ticket page</a>.</p>
            <p>We value your perspective. Please take a moment to <a href="{feedback_url}">share feedback on this experience</a>.</p>
            <p>Thank you,<br>Seegars IT</p>
            """
        else:
            subject = f"Ticket status update: {ticket_title}"
            body_html = f"""
            <p>Hi {escape(requester_name)},</p>
            <p>Your ticket <strong>{escape(ticket_title)}</strong> is now marked <strong>{escape(status)}</strong>.</p>
            <p><a href="{ticket_link}">Open your ticket</a> to review progress or add more information.</p>
            <p>Thank you,<br>Seegars IT</p>
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
    db = get_db()
    ticket_row = db.execute(
        """
        SELECT assignee, title, requester_name, requester_email
        FROM tickets
        WHERE id = ?
        """,
        (ticket_id,),
    ).fetchone()
    if not ticket_row:
        flash("Ticket not found.")
        return redirect(url_for("tickets"))
    previous_assignee = (ticket_row["assignee"] or "").strip()
    db.execute(
        "UPDATE tickets SET assignee = ?, updated_at = ? WHERE id = ?",
        (assignee, now_ts(), ticket_id),
    )
    db.commit()
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
        <p>Thank you,<br>Seegars IT</p>
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
    return redirect(url_for("tickets"))

# --------------------------------------------------------------------------------------
# Jinja loader (since we keep templates inline in this single file)
# --------------------------------------------------------------------------------------
app.jinja_loader = DictLoader({
    "base.html": BASE_HTML,
    "home.html": HOME_HTML,   # ← add this line
    "dashboard.html": DASHBOARD_HTML,
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
