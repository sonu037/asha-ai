"""Validate Batch A source registration and apply safe, repeatable DB migrations."""

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
APP_DIR = ROOT / "app"
REQUIRED_FILES = (
    APP_DIR / "__init__.py",
    APP_DIR / "main.py",
    APP_DIR / "batch_a.py",
    APP_DIR / "db.py",
)
REQUIRED_ROUTES = {
    "/search",
    "/documents/{document_id}/create-verified-event",
    "/documents/{document_id}/match-candidates",
    "/people/{person_id}/health-summary",
}


def main() -> int:
    missing = [str(path) for path in REQUIRED_FILES if not path.is_file()]
    if missing:
        print(
            "Batch A installation cannot continue; required project files are missing:\n"
            + "\n".join(missing),
            file=sys.stderr,
        )
        return 1

    sys.path.insert(0, str(ROOT))
    from app.db import init_db
    from app.main import app

    registered_routes = set(app.openapi()["paths"])
    missing_routes = sorted(REQUIRED_ROUTES - registered_routes)
    if missing_routes:
        print(
            "Batch A routes are not registered in app/main.py: "
            + ", ".join(missing_routes),
            file=sys.stderr,
        )
        return 1

    init_db()
    print("Batch A API routes are registered and database migrations are current.")
    print("No application source files were rewritten.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
