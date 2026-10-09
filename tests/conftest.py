import pytest


@pytest.fixture(autouse=True)
def isolate_application_data(tmp_path, monkeypatch):
    from app import db, main

    test_root = tmp_path / "app-data"
    document_dir = test_root / "documents"
    document_dir.mkdir(parents=True)
    monkeypatch.setattr(db, "DB_PATH", test_root / "test.db")
    monkeypatch.setattr(main, "DOCUMENT_DIR", document_dir)
    db.init_db()
