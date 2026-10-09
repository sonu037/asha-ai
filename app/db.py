import sqlite3
import warnings
from contextlib import contextmanager
from .config import DB_PATH


def _create_unique_index_if_clean(conn, duplicate_query, create_query, index_name):
    duplicate_groups = conn.execute(duplicate_query).fetchone()[0]
    if duplicate_groups:
        warnings.warn(
            f"Skipped {index_name}: existing duplicate rows need review before "
            "uniqueness can be enforced.",
            RuntimeWarning,
            stacklevel=2,
        )
        return
    conn.execute(create_query)

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
 original_filename TEXT, storage_path TEXT NOT NULL, content_hash TEXT, captured_at TEXT, ocr_text TEXT, language TEXT,
 extraction_status TEXT NOT NULL DEFAULT 'pending', verification_status TEXT NOT NULL DEFAULT 'unverified',
 created_by TEXT, extraction_provider TEXT, extraction_model TEXT, processing_version TEXT,
 extraction_warnings TEXT, classification_confidence REAL,
 classification_needs_review INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS document_fields (
 id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER NOT NULL, field_name TEXT NOT NULL,
 extracted_value TEXT, confidence REAL, page_number INTEGER, bounding_box TEXT, extraction_method TEXT,
 verified_value TEXT, verified_by TEXT, verified_at TEXT, needs_review INTEGER NOT NULL DEFAULT 0,
 review_reason TEXT, source_text TEXT, FOREIGN KEY (document_id) REFERENCES documents(id)
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
CREATE TABLE IF NOT EXISTS document_field_conflicts (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 person_id INTEGER NOT NULL,
 field_name TEXT NOT NULL,
 field_id_a INTEGER NOT NULL,
 document_id_a INTEGER NOT NULL,
 field_value_a TEXT NOT NULL,
 field_id_b INTEGER NOT NULL,
 document_id_b INTEGER NOT NULL,
 field_value_b TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'open',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(field_id_a, field_id_b),
 FOREIGN KEY (person_id) REFERENCES people(id),
 FOREIGN KEY (field_id_a) REFERENCES document_fields(id),
 FOREIGN KEY (document_id_a) REFERENCES documents(id),
 FOREIGN KEY (field_id_b) REFERENCES document_fields(id),
 FOREIGN KEY (document_id_b) REFERENCES documents(id)
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
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with connection() as conn:
        conn.executescript(SCHEMA)

        document_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(documents)").fetchall()
        }
        if "content_hash" not in document_cols:
            conn.execute("ALTER TABLE documents ADD COLUMN content_hash TEXT")
        if "created_by" not in document_cols:
            conn.execute("ALTER TABLE documents ADD COLUMN created_by TEXT")
        document_migrations = {
            "extraction_provider": "TEXT",
            "extraction_model": "TEXT",
            "processing_version": "TEXT",
            "extraction_warnings": "TEXT",
            "classification_confidence": "REAL",
            "classification_needs_review": (
                "INTEGER NOT NULL DEFAULT 1"
            ),
        }
        for column, column_type in document_migrations.items():
            if column not in document_cols:
                conn.execute(
                    f"ALTER TABLE documents ADD COLUMN {column} {column_type}"
                )
        field_cols = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(document_fields)"
            ).fetchall()
        }
        if "source_text" not in field_cols:
            conn.execute(
                "ALTER TABLE document_fields ADD COLUMN source_text TEXT"
            )
        _create_unique_index_if_clean(
            conn,
            """SELECT COUNT(*) FROM (
                   SELECT content_hash FROM documents
                   WHERE content_hash IS NOT NULL
                   GROUP BY content_hash HAVING COUNT(*) > 1
               )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_content_hash
               ON documents(content_hash) WHERE content_hash IS NOT NULL""",
            "idx_documents_content_hash",
        )

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
        _create_unique_index_if_clean(
            conn,
            """SELECT COUNT(*) FROM (
                   SELECT source_document_id, person_id, event_type,
                          COALESCE(event_date, ''), COALESCE(details_json, '')
                   FROM events
                   WHERE source_document_id IS NOT NULL AND person_id IS NOT NULL
                   GROUP BY source_document_id, person_id, event_type,
                            COALESCE(event_date, ''), COALESCE(details_json, '')
                   HAVING COUNT(*) > 1
               )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_events_document_dedup
               ON events(
                   source_document_id,
                   person_id,
                   event_type,
                   COALESCE(event_date, ''),
                   COALESCE(details_json, '')
               )
               WHERE source_document_id IS NOT NULL AND person_id IS NOT NULL""",
            "idx_events_document_dedup",
        )
        _create_unique_index_if_clean(
            conn,
            """SELECT COUNT(*) FROM (
                   SELECT COALESCE(person_id, -1), COALESCE(household_id, -1),
                          record_type, field_name, value,
                          COALESCE(source_document_id, -1)
                   FROM canonical_records
                   GROUP BY COALESCE(person_id, -1), COALESCE(household_id, -1),
                            record_type, field_name, value,
                            COALESCE(source_document_id, -1)
                   HAVING COUNT(*) > 1
               )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_canonical_source_dedup
               ON canonical_records(
                   COALESCE(person_id, -1),
                   COALESCE(household_id, -1),
                   record_type,
                   field_name,
                   value,
                   COALESCE(source_document_id, -1)
               )""",
            "idx_canonical_source_dedup",
        )