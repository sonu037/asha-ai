import sqlite3
from contextlib import contextmanager
from .config import DB_PATH

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS households (
 id INTEGER PRIMARY KEY AUTOINCREMENT, external_id TEXT UNIQUE NOT NULL, village_id TEXT,
 address TEXT, head_person_id INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS people (
 id INTEGER PRIMARY KEY AUTOINCREMENT, external_id TEXT UNIQUE NOT NULL, household_id INTEGER NOT NULL,
 name TEXT NOT NULL, sex TEXT, date_of_birth TEXT, relationship_to_head TEXT,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 FOREIGN KEY (household_id) REFERENCES households(id)
);
CREATE TABLE IF NOT EXISTS documents (
 id INTEGER PRIMARY KEY AUTOINCREMENT, external_id TEXT UNIQUE NOT NULL, document_type TEXT,
 original_filename TEXT, storage_path TEXT NOT NULL, captured_at TEXT, ocr_text TEXT, language TEXT,
 extraction_status TEXT NOT NULL DEFAULT 'pending', verification_status TEXT NOT NULL DEFAULT 'unverified',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS document_fields (
 id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER NOT NULL, field_name TEXT NOT NULL,
 extracted_value TEXT, confidence REAL, page_number INTEGER, bounding_box TEXT, extraction_method TEXT,
 verified_value TEXT, verified_by TEXT, verified_at TEXT, needs_review INTEGER NOT NULL DEFAULT 0,
 review_reason TEXT, FOREIGN KEY (document_id) REFERENCES documents(id)
);
CREATE TABLE IF NOT EXISTS canonical_records (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 person_id INTEGER,
 household_id INTEGER,
 record_type TEXT NOT NULL,
 field_name TEXT NOT NULL,
 value TEXT NOT NULL,
 verified_by TEXT NOT NULL,
 verified_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 source_document_id INTEGER,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 FOREIGN KEY (person_id) REFERENCES people(id),
 FOREIGN KEY (household_id) REFERENCES households(id),
 FOREIGN KEY (source_document_id) REFERENCES documents(id)
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, external_id TEXT UNIQUE NOT NULL, household_id INTEGER,
 person_id INTEGER, asha_id TEXT, event_type TEXT NOT NULL, event_date TEXT, details_json TEXT,
 source_document_id INTEGER, verification_status TEXT NOT NULL DEFAULT 'unverified',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 FOREIGN KEY (household_id) REFERENCES households(id), FOREIGN KEY (person_id) REFERENCES people(id),
 FOREIGN KEY (source_document_id) REFERENCES documents(id)
);
CREATE TABLE IF NOT EXISTS document_person_links (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 document_id INTEGER NOT NULL,
 person_id INTEGER NOT NULL,
 linked_by TEXT NOT NULL,
 linked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(document_id),
 FOREIGN KEY (document_id) REFERENCES documents(id),
 FOREIGN KEY (person_id) REFERENCES people(id)
);
CREATE TABLE IF NOT EXISTS document_person_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    person_id INTEGER NOT NULL,
    linked_by TEXT NOT NULL,
    linked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(document_id),
    FOREIGN KEY (document_id) REFERENCES documents(id),
    FOREIGN KEY (person_id) REFERENCES people(id)
);
CREATE TABLE IF NOT EXISTS audit_log (
 id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id TEXT, action TEXT NOT NULL, entity_type TEXT NOT NULL,
 entity_id TEXT NOT NULL, details_json TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

@contextmanager
def connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with connection() as conn:
        conn.executescript(SCHEMA)

        # Lightweight forward migration for v0.2.
        cols = {
            r['name']
            for r in conn.execute(
                'PRAGMA table_info(document_fields)'
            ).fetchall()
        }

        if 'needs_review' not in cols:
            conn.execute(
                'ALTER TABLE document_fields '
                'ADD COLUMN needs_review INTEGER NOT NULL DEFAULT 0'
            )

        if 'review_reason' not in cols:
            conn.execute(
                'ALTER TABLE document_fields '
                'ADD COLUMN review_reason TEXT'
            )

        # Forward migration for event verification provenance.
        event_cols = {
            r['name']
            for r in conn.execute(
                'PRAGMA table_info(events)'
            ).fetchall()
        }

        if 'verified_by' not in event_cols:
            conn.execute(
                'ALTER TABLE events ADD COLUMN verified_by TEXT'
            )

        if 'verified_at' not in event_cols:
            conn.execute(
                'ALTER TABLE events ADD COLUMN verified_at TEXT'
            )