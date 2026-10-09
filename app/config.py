from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "asha_ai.db"
DOCUMENT_DIR = BASE_DIR / "storage" / "documents"
DOCUMENT_DIR.mkdir(parents=True, exist_ok=True)
