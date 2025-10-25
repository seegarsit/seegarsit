import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
import app as seegars_app  # noqa: E402


def configure_isolated_db(monkeypatch, tmp_path):
    db_path = tmp_path / "tickets.db"
    monkeypatch.setattr(seegars_app, "DB_PATH", str(db_path))
    if "TICKETS_DB" in os.environ:
        monkeypatch.delenv("TICKETS_DB", raising=False)
    with seegars_app.app.app_context():
        seegars_app.init_db()
    return db_path


def test_init_db_preserves_existing_tickets(monkeypatch, tmp_path):
    configure_isolated_db(monkeypatch, tmp_path)

    with seegars_app.app.app_context():
        db = seegars_app.get_db()
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            """
            INSERT INTO tickets (
                title, description, requester_name, requester_email, branch, priority,
                category, assignee, status, created_at, updated_at, completed_at, feedback_token
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Original Ticket",
                "Initial description",
                "Employee",
                "employee@example.com",
                "Cary",
                "Medium",
                "Network / Internet",
                "Brad Wells",
                "Open",
                now,
                now,
                None,
                None,
            ),
        )
        db.commit()

        seegars_app.init_db()

        rows = db.execute(
            "SELECT title, feedback_token FROM tickets ORDER BY id"
        ).fetchall()

    assert len(rows) == 1
    assert rows[0]["title"] == "Original Ticket"
    assert rows[0]["feedback_token"]
