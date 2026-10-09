import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def isolate_application_data(tmp_path, monkeypatch):
    from app import db, main

    test_root = tmp_path / "app-data"
    document_dir = test_root / "documents"
    document_dir.mkdir(parents=True)
    monkeypatch.setattr(db, "DB_PATH", test_root / "test.db")
    monkeypatch.setattr(main, "DOCUMENT_DIR", document_dir)
    db.init_db()


@pytest.fixture
def client():
    from app.db import init_db
    from app.main import app

    init_db()
    with TestClient(app) as test_client:
        yield test_client
