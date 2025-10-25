import os
import sys
from datetime import datetime, timezone
from pathlib import Path
import uuid

sys.path.append(str(Path(__file__).resolve().parents[1]))
import app as seegars_app  # noqa: E402


def seed_ticket(db, **overrides):
    defaults = {
        "title": "Sample Ticket",
        "description": "Details",
        "requester_name": "Requester",
        "requester_email": "user@example.com",
        "branch": "Cary",
        "priority": "Medium",
        "category": "Network / Internet",
        "assignee": "Brad Wells",
        "status": "Open",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "feedback_token": uuid.uuid4().hex,
    }
    defaults.update(overrides)
    db.execute(
        """
        INSERT INTO tickets (
            title, description, requester_name, requester_email, branch, priority,
            category, assignee, status, created_at, updated_at, completed_at, feedback_token
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            defaults["title"],
            defaults["description"],
            defaults["requester_name"],
            defaults["requester_email"],
            defaults["branch"],
            defaults["priority"],
            defaults["category"],
            defaults["assignee"],
            defaults["status"],
            defaults["created_at"],
            defaults["updated_at"],
            defaults["completed_at"],
            defaults["feedback_token"],
        ),
    )


def configure_db(monkeypatch, tmp_path):
    db_path = tmp_path / "tickets.db"
    monkeypatch.setattr(seegars_app, "DB_PATH", str(db_path))
    if "TICKETS_DB" in os.environ:
        monkeypatch.delenv("TICKETS_DB", raising=False)
    with seegars_app.app.app_context():
        seegars_app.init_db()
    return db_path


def test_admin_sees_all_tickets(monkeypatch, tmp_path):
    configure_db(monkeypatch, tmp_path)
    admin_email = next(iter(seegars_app.ADMIN_EMAILS))

    with seegars_app.app.app_context():
        db = seegars_app.get_db()
        seed_ticket(db, title="Branch Issue", requester_email="user1@example.com")
        seed_ticket(db, title="Printer Down", requester_email="user2@example.com")
        db.commit()

    client = seegars_app.app.test_client()
    with client.session_transaction() as session:
        session["user"] = {"email": admin_email}

    response = client.get("/tickets")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Branch Issue" in body
    assert "Printer Down" in body


def test_regular_user_sees_only_their_tickets(monkeypatch, tmp_path):
    configure_db(monkeypatch, tmp_path)

    with seegars_app.app.app_context():
        db = seegars_app.get_db()
        seed_ticket(db, title="WiFi Failure", requester_email="employee@seegars.com")
        seed_ticket(db, title="Accounting Software", requester_email="other@seegars.com")
        db.commit()

    client = seegars_app.app.test_client()
    with client.session_transaction() as session:
        session["user"] = {"email": "employee@seegars.com"}

    response = client.get("/tickets")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "WiFi Failure" in body
    assert "Accounting Software" not in body
