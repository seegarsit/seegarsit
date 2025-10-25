from pathlib import Path

import app as seegars_app


def test_default_path_uses_render_data_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("TICKETS_DB", raising=False)
    resolved_path = seegars_app._resolve_db_path(data_dir_override=str(tmp_path))
    path_obj = Path(resolved_path)

    assert path_obj.parent == tmp_path
    assert path_obj.name == "tickets.db"
    assert path_obj.parent.exists()


def test_env_override_for_memory_database(monkeypatch):
    monkeypatch.setenv("TICKETS_DB", ":memory:")
    resolved = seegars_app._resolve_db_path()

    assert resolved == ":memory:"


def test_relative_path_is_rooted_in_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TICKETS_DB", "nested/tickets.sqlite")
    resolved = seegars_app._resolve_db_path(data_dir_override=str(tmp_path))
    path_obj = Path(resolved)

    assert path_obj.parent == tmp_path / "nested"
    assert path_obj.name == "tickets.sqlite"
    assert path_obj.parent.exists()
