from __future__ import annotations
import os
import sqlite3
from datetime import datetime
from typing import Optional
from functools import wraps

from flask import Flask, g, redirect, render_template_string, request, url_for, flash, session
from jinja2 import DictLoader
from msal import ConfidentialClientApplication
import uuid
import requests

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
            updated_at TIMESTAMP NOT NULL
        );

        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER NOT NULL,
            author TEXT,
            body TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL,
            FOREIGN KEY(ticket_id) REFERENCES tickets(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
        CREATE INDEX IF NOT EXISTS idx_tickets_priority ON tickets(priority);
        CREATE INDEX IF NOT EXISTS idx_tickets_branch ON tickets(branch);
        CREATE INDEX IF NOT EXISTS idx_comments_ticket ON comments(ticket_id);
        """
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
PRIORITIES = ["Low", "Medium", "High"]
ASSIGNEE_DEFAULT = "Brad Wells"
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

def now_ts():
    return datetime.utcnow()

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
  <!-- Brand font (fallbacks if CoFo Gothic isn’t available) -->
  <link href="https://fonts.googleapis.com/css2?family=Open+Sans:wght@400;600;700&display=swap" rel="stylesheet">
  <style>
    :root{
      --sg-black:#231F20;
      --sg-green:#008752;
      --sg-lime:#BCD531;
      --sg-gray:#DEE0D9;
      --sg-offwhite:#F5F6F4;
    }
    html,body{ height:100%; }
    body{
      background:var(--sg-offwhite);
      color:var(--sg-black);
      font-family:"CoFo Gothic","Open Sans","Segoe UI","Helvetica Neue",Arial,sans-serif;
      -webkit-font-smoothing:antialiased; -moz-osx-font-smoothing:grayscale;
    }
    .navbar{
      background:var(--sg-black)!important;
      border-bottom:4px solid var(--sg-green);
    }
    .navbar .navbar-brand{
      font-weight:700; letter-spacing:.2px;
      color:#fff!important;
    }
    .navbar .btn-primary{
      background:var(--sg-green); border-color:var(--sg-green);
    }
    .navbar .btn-primary:hover{
      background:#006e43; border-color:#006e43;
    }
    .card{
      background:#fff; border:1px solid var(--sg-gray);
      box-shadow:0 6px 18px rgba(0,0,0,.035);
    }
    .form-control, .form-select{
      background:#fff; color:var(--sg-black); border-color:var(--sg-gray);
    }
    .form-control:focus, .form-select:focus{
      border-color:var(--sg-green);
      box-shadow:0 0 0 .2rem rgba(0,135,82,.15);
    }
    a{ color:var(--sg-green); text-decoration:none; }
    a:hover{ color:#006e43; }
    .btn-primary{
      background:var(--sg-green); border-color:var(--sg-green);
    }
    .btn-primary:hover{
      background:#006e43; border-color:#006e43;
    }
    .btn-outline-light{
      color:var(--sg-black); border-color:var(--sg-black);
    }
    .badge.text-bg-danger{ background:#c53030!important; }
    .badge.text-bg-warning{ background:var(--sg-lime)!important; color:#1b1b1b; }
    .badge.text-bg-primary{ background:var(--sg-green)!important; }
    .table > :not(caption) > * > * { background: transparent; }
    .text-secondary{ color:#676a6d!important; }
  </style>
</head>
<body>
<nav class="navbar navbar-expand-lg navbar-dark mb-4">
  <div class="container">
    <a class="navbar-brand" href="{{ url_for('list_tickets') }}">Seegars Fence Company Tickets</a>
    <div class="ms-auto d-flex align-items-center gap-2">
      {% if session.get('user') %}
        <span class="small text-secondary">Signed in as {{ session['user']['name'] or session['user']['email'] }}</span>
        <a class="btn btn-outline-light btn-sm" href="{{ url_for('logout') }}">Logout</a>
      {% else %}
        <a class="btn btn-primary btn-sm" href="{{ url_for('login') }}">Login with Microsoft</a>
      {% endif %}
      <a class="btn btn-primary" href="{{ url_for('new_ticket') }}">+ New Ticket</a>
    </div>
  </div>
</nav>
<main class="container">
  {% with messages = get_flashed_messages() %}
    {% if messages %}
      <div class="alert alert-info">{{ messages|join('\\n') }}</div>
    {% endif %}
  {% endwith %}
  {% block content %}{% endblock %}
</main>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""


INDEX_HTML = """
{% extends 'base.html' %}
{% block content %}
<div class=\"card p-3 mb-4\">
  <form class=\"row g-2\" method=\"get\">
    <div class=\"col-md-3\">
      <input name=\"q\" value=\"{{ request.args.get('q','') }}\" class=\"form-control\" placeholder=\"Search title/description/assignee…\">
    </div>
    <div class=\"col-md-2\">
      <select name=\"status\" class=\"form-select\">
        <option value=\"\">All Statuses</option>
        {% for s in statuses %}
        <option value=\"{{s}}\" {% if request.args.get('status')==s %}selected{% endif %}>{{s}}</option>
        {% endfor %}
      </select>
    </div>
    <div class=\"col-md-2\">
      <select name=\"priority\" class=\"form-select\">
        <option value=\"\">All Priorities</option>
        {% for p in priorities %}
        <option value=\"{{p}}\" {% if request.args.get('priority')==p %}selected{% endif %}>{{p}}</option>
        {% endfor %}
      </select>
    </div>
    <div class=\"col-md-2\">
      <input name=\"branch\" value=\"{{ request.args.get('branch','') }}\" class=\"form-control\" placeholder=\"Branch\">
    </div>
    <div class=\"col-md-2\">
      <select name=\"sort\" class=\"form-select\">
        <option value=\"new\">Newest</option>
        <option value=\"old\" {% if request.args.get('sort')=='old' %}selected{% endif %}>Oldest</option>
        <option value=\"priority\" {% if request.args.get('sort')=='priority' %}selected{% endif %}>Priority</option>
        <option value=\"status\" {% if request.args.get('sort')=='status' %}selected{% endif %}>Status</option>
      </select>
    </div>
    <div class=\"col-md-1 d-grid\">
      <button class=\"btn btn-primary\" type=\"submit\">Filter</button>
    </div>
  </form>
</div>

<div class=\"card p-0\">
  <div class=\"table-responsive\">
  <table class=\"table align-middle mb-0\">
    <thead>
      <tr>
        <th>ID</th><th>Title</th><th>Branch</th><th>Priority</th><th>Status</th><th>Assignee</th><th>Updated</th>
      </tr>
    </thead>
    <tbody>
      {% for t in tickets %}
      <tr>
        <td><a href=\"{{ url_for('ticket_detail', ticket_id=t['id']) }}\">#{{ t['id'] }}</a></td>
        <td><a href=\"{{ url_for('ticket_detail', ticket_id=t['id']) }}\">{{ t['title'] }}</a></td>
        <td>{{ t['branch'] or '—' }}</td>
        <td>
          <span class=\"badge text-bg-{% if t['priority']=='High' %}danger{% elif t['priority']=='Medium' %}warning{% else %}secondary{% endif %}\">{{ t['priority'] }}</span>
        </td>
        <td>
          <span class=\"badge text-bg-{% if t['status'] in ['Open','In Progress'] %}primary{% elif t['status']=='Waiting' %}warning{% elif t['status']=='Resolved' %}success{% else %}secondary{% endif %}\">{{ t['status'] }}</span>
        </td>
        <td>{{ t['assignee'] or '—' }}</td>
        <td>{{ t['updated_at'] }}</td>
      </tr>
      {% else %}
      <tr><td colspan=\"7\" class=\"text-center py-4\">No tickets found.</td></tr>
      {% endfor %}
    </tbody>
  </table>
  </div>
</div>
{% endblock %}
"""

NEW_HTML = """
{% extends 'base.html' %}
{% block content %}
<div class="card p-4">
  <h3 class="mb-3">Create Ticket</h3>
  {% if not session.get('user') %}
    <div class="alert alert-warning">Please <a href="{{ url_for('login') }}">sign in with Microsoft</a> to submit a ticket.</div>
  {% endif %}
  <form method="post">
    <div class="row g-3">
      <div class="col-md-8">
        <label class="form-label">Title</label>
        <input name="title" class="form-control" required>
      </div>
      <div class="col-md-4">
        <label class="form-label">Priority</label>
<small class="text-muted"><i>(select option from dropdown list)</i></small>
<select name="priority" class="form-select">
          <option value="Low">Low — General request or question</option>
          <option value="Medium">Medium — Interferes with productivity</option>
          <option value="High">High — Stops work / critical system issue</option>
        </select>
      </div>
      <div class="col-md-12">
       <label class="form-label">Description</label>
<small class="text-muted"><i>(include as many details as possible)</i></small>
<textarea name="description" class="form-control" rows="5" required></textarea>
      </div>
      <div class="col-md-4">
        <label class="form-label">Requester Name</label>
        <input name="requester_name" class="form-control" value="{{ session.get('user', {}).get('name','') }}">
      </div>
      <div class="col-md-4">
        <label class="form-label">Requester Email</label>
        <input type="email" name="requester_email" class="form-control" value="{{ session.get('user', {}).get('email','') }}">
      </div>
      <div class="col-md-4">
      <label class="form-label">Branch</label>
<small class="text-muted"><i>(select option from dropdown list)</i></small>
<select name="branch" class="form-select">
          {% for b in branches %}
            <option value="{{ b }}">{{ b }}</option>
          {% endfor %}
        </select>
      </div>
      <div class="col-md-6">
       <label class="form-label">Category</label>
<small class="text-muted"><i>(select option from dropdown list)</i></small>
<select name="category" class="form-select">
          {% for c in categories %}
            <option value="{{ c }}">{{ c }}</option>
          {% endfor %}
        </select>
      </div>
    </div>
    <div class="mt-4 d-flex gap-2">
      <button class="btn btn-primary" type="submit">Create</button>
      <a class="btn btn-outline-light" href="{{ url_for('list_tickets') }}">Cancel</a>
    </div>
  </form>
</div>
{% endblock %}
"""


DETAIL_HTML = """
{% extends 'base.html' %}
{% block content %}
<div class=\"row g-3\">
  <div class=\"col-lg-8\">
    <div class=\"card p-4\">
      <div class=\"d-flex justify-content-between align-items-start\">
        <h3 class=\"mb-1\">#{{ t['id'] }} – {{ t['title'] }}</h3>
        <span class=\"badge text-bg-{% if t['priority']=='High' %}danger{% elif t['priority']=='Medium' %}warning{% else %}secondary{% endif %}\">{{ t['priority'] }}</span>
      </div>
      <p class=\"text-secondary\">Created {{ t['created_at'] }} • Updated {{ t['updated_at'] }}</p>
      <p class=\"mb-3\">{{ t['description']|replace('\\n','<br>')|safe }}</p>

      <div class=\"row g-3\">
        <div class=\"col-md-6\">
          <div class=\"small text-secondary\">Requester</div>
          <div>{{ t['requester_name'] or '—' }} {% if t['requester_email'] %}• <a href=\"mailto:{{t['requester_email']}}\">{{ t['requester_email'] }}</a>{% endif %}</div>
        </div>
        <div class=\"col-md-3\">
          <div class=\"small text-secondary\">Branch</div>
          <div>{{ t['branch'] or '—' }}</div>
        </div>
        <div class=\"col-md-3\">
          <div class=\"small text-secondary\">Category</div>
          <div>{{ t['category'] or '—' }}</div>
        </div>
      </div>

      <hr>
      <h5 class=\"mb-3\">Comments</h5>
      {% for c in comments %}
        <div class=\"mb-3\">
          <div class=\"small text-secondary\">{{ c['created_at'] }} • {{ c['author'] or 'Anonymous' }}</div>
          <div>{{ c['body']|replace('\\n','<br>')|safe }}</div>
        </div>
      {% else %}
        <p class=\"text-secondary\">No comments yet.</p>
      {% endfor %}

      <form method=\"post\" action=\"{{ url_for('add_comment', ticket_id=t['id']) }}\" class=\"mt-3\">
        <div class=\"row g-2\">
          <div class=\"col-md-3\"><input class=\"form-control\" name=\"author\" placeholder=\"Your name\"></div>
          <div class=\"col-md-9\"><textarea class=\"form-control\" name=\"body\" rows=\"3\" placeholder=\"Add a comment…\" required></textarea></div>
        </div>
        <div class=\"mt-2\"><button class=\"btn btn-primary\">Post Comment</button></div>
      </form>
    </div>
  </div>
  <div class=\"col-lg-4\">
    <div class=\"card p-3\">
      <h5 class=\"mb-3\">Ticket Controls</h5>
      <form method=\"post\" action=\"{{ url_for('update_status', ticket_id=t['id']) }}\" class=\"mb-3\">
        <label class=\"form-label\">Status</label>
        <div class=\"d-flex gap-2\">
          <select name=\"status\" class=\"form-select\">
            {% for s in statuses %}<option value=\"{{s}}\" {% if s==t['status'] %}selected{% endif %}>{{s}}</option>{% endfor %}
          </select>
          <button class=\"btn btn-primary\" type=\"submit\">Update</button>
        </div>
      </form>

      <form method=\"post\" action=\"{{ url_for('update_assignee', ticket_id=t['id']) }}\">
        <label class=\"form-label\">Assignee</label>
        <div class=\"d-flex gap-2\">
          <input class=\"form-control\" name=\"assignee\" value=\"{{ t['assignee'] or '' }}\" placeholder=\"e.g., Brad Wells\">
          <button class=\"btn btn-outline-light\" type=\"submit\">Save</button>
        </div>
      </form>
    </div>
  </div>
</div>
{% endblock %}
"""

# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------

@app.route("/")
def list_tickets():
    with app.app_context():
        init_db()
    db = get_db()
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    priority = request.args.get("priority", "").strip()
    branch = request.args.get("branch", "").strip()
    sort = request.args.get("sort", "new")

    sql = "SELECT * FROM tickets WHERE 1=1"
    params: list = []
    if q:
        sql += " AND (title LIKE ? OR description LIKE ? OR assignee LIKE ?)"
        like = f"%{q}%"
        params += [like, like, like]
    if status:
        sql += " AND status = ?"
        params.append(status)
    if priority:
        sql += " AND priority = ?"
        params.append(priority)
    if branch:
        sql += " AND branch LIKE ?"
        params.append(f"%{branch}%")

    order = {
        "new": "updated_at DESC",
        "old": "updated_at ASC",
        "priority": "CASE priority WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END, updated_at DESC",
        "status": "status ASC, updated_at DESC",
    }.get(sort, "updated_at DESC")
    sql += f" ORDER BY {order}"

    tickets = db.execute(sql, params).fetchall()
    return render_template_string(INDEX_HTML, tickets=tickets, statuses=STATUSES, priorities=PRIORITIES)


@app.route("/new", methods=["GET", "POST"])
@login_required
def new_ticket():
    if request.method == "POST":
        data = {k: (request.form.get(k) or "").strip() for k in [
            "title", "description", "requester_name", "requester_email", "branch", "priority", "category"
        ]}

        if not data["title"] or not data["description"]:
            flash("Title and Description are required.")
            return redirect(url_for("new_ticket"))

        ts = datetime.utcnow().isoformat()
        db = get_db()
        cur = db.execute(
            """
            INSERT INTO tickets (title, description, requester_name, requester_email, branch, priority, category, assignee, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Open', ?, ?)
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
                ts
            )
        )
        ticket_id = cur.lastrowid
        db.commit()

        # --- Email notifications (uses session['access_token']) ---
        subject_admin = f"[Ticket #{ticket_id}] {data['title']}"
        body_admin = f"""
        <p><b>New ticket created</b></p>
        <p><b>Title:</b> {data['title']}<br>
        <b>Priority:</b> {data['priority']}<br>
        <b>Branch:</b> {data['branch']}<br>
        <b>Category:</b> {data['category']}<br>
        <b>Requester:</b> {data['requester_name']} &lt;{data['requester_email']}&gt;</p>
        <p><b>Description:</b><br>{data['description'].replace('\n','<br>')}</p>
        """

        send_email("brad@seegarsfence.com", subject_admin, body_admin)

        if data["requester_email"]:
            subject_user = f"We received your ticket #{ticket_id}: {data['title']}"
            body_user = f"""
            <p>Hi {data['requester_name'] or ''},</p>
            <p>Your ticket has been received by Seegars IT. We’ll follow up soon.</p>
            <p><b>Summary</b><br>
            <b>Priority:</b> {data['priority']}<br>
            <b>Branch:</b> {data['branch']}<br>
            <b>Category:</b> {data['category']}</p>
            <p><b>Description:</b><br>{data['description'].replace('\n','<br>')}</p>
            """
            send_email(data["requester_email"], subject_user, body_user)

        flash("Ticket created successfully!")
        return redirect(url_for("list_tickets"))

    # GET request: show the form
    return render_template_string(
        NEW_HTML,
        priorities=PRIORITIES,
        branches=BRANCHES,
        categories=CATEGORIES
    )


@app.route("/ticket/<int:ticket_id>")
def ticket_detail(ticket_id: int):
    with app.app_context():
        init_db()
    db = get_db()
    t = db.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if not t:
        flash("Ticket not found.")
        return redirect(url_for("list_tickets"))
    comments = db.execute(
        "SELECT * FROM comments WHERE ticket_id = ? ORDER BY created_at ASC",
        (ticket_id,)
    ).fetchall()
    return render_template_string(DETAIL_HTML, t=t, comments=comments, statuses=STATUSES)


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
    flash("Comment added.")
    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


@app.route("/ticket/<int:ticket_id>/status", methods=["POST"])
@login_required
def update_status(ticket_id: int):
    with app.app_context():
        init_db()
    status = (request.form.get("status") or "Open").strip()
    if status not in STATUSES:
        status = "Open"
    db = get_db()
    db.execute(
        "UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?",
        (status, now_ts(), ticket_id),
    )
    db.commit()
    flash("Status updated.")
    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


@app.route("/ticket/<int:ticket_id>/assignee", methods=["POST"])
@login_required
def update_assignee(ticket_id: int):
    with app.app_context():
        init_db()
    assignee = (request.form.get("assignee") or "").strip()
    db = get_db()
    db.execute(
        "UPDATE tickets SET assignee = ?, updated_at = ? WHERE id = ?",
        (assignee, now_ts(), ticket_id),
    )
    db.commit()
    flash("Assignee updated.")
    return redirect(url_for("ticket_detail", ticket_id=ticket_id))

# --------------------------------------------------------------------------------------
# Microsoft 365 sign-in routes
# --------------------------------------------------------------------------------------

@app.route("/login")
def login():
    # If env vars not set, show a friendly error
    if not (CLIENT_ID and CLIENT_SECRET and AUTHORITY and REDIRECT_URI):
        flash("Microsoft login not configured. Set env vars on Render.")
        return redirect(url_for("list_tickets"))
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
        return redirect(url_for("list_tickets"))

    result = msal_app().acquire_token_by_authorization_code(
        code,
        scopes=SCOPE,
        redirect_uri=REDIRECT_URI,
    )
    session["access_token"] = result.get("access_token")

    if "id_token_claims" not in result:
        flash("Login failed.")
        return redirect(url_for("list_tickets"))

    claims = result["id_token_claims"]
    session["user"] = {
        "name": claims.get("name") or claims.get("preferred_username"),
        "email": claims.get("preferred_username"),
        "oid": claims.get("oid"),
    }
    flash(f"Signed in as {session['user']['email']}")
    return redirect(url_for("list_tickets"))

@app.route("/logout")
def logout():
    session.clear()
    flash("Signed out.")
    return redirect(url_for("list_tickets"))

# --------------------------------------------------------------------------------------
# Jinja loader (since we keep templates inline in this single file)
# --------------------------------------------------------------------------------------
app.jinja_loader = DictLoader({
    "base.html": BASE_HTML,
    "index.html": INDEX_HTML,
    "new.html": NEW_HTML,
    "detail.html": DETAIL_HTML,
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
