"""Apply pending migrations to a database that may never have been stamped.

This project creates fresh databases from ``init.sql`` and evolves existing
ones with Alembic. That works until the two meet: a database built by
``init.sql`` already has the tables, but no ``alembic_version`` row, so a plain
``alembic upgrade head`` starts at 0001 and dies on "table already exists".

So: probe what the schema actually contains, stamp that revision without
running it, and upgrade from there. A database that already carries a stamp is
left to Alembic, and an empty one is migrated from scratch.

Run it through ``make migrate``.
"""

from __future__ import annotations

import os
import sys

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

#: Newest first. Each entry is the revision and a probe that is true once that
#: revision has been applied — a table or column it introduced.
_LEVELS: list[tuple[str, str]] = [
    ("0010", "SELECT to_regclass('public.usecase_skill_functions') IS NOT NULL"),
    ("0009", "SELECT to_regclass('public.api_jobs') IS NOT NULL"),
    ("0008", "SELECT to_regclass('public.deleted_use_cases') IS NOT NULL"),
    ("0007", "SELECT to_regclass('public.user_use_cases') IS NOT NULL"),
    ("0006", "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
             "WHERE table_name='queries' AND column_name='images')"),
    ("0005", "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
             "WHERE table_name='queries' AND column_name='citations')"),
    ("0004", "SELECT to_regclass('public.use_cases') IS NOT NULL"),
    ("0003", "SELECT to_regclass('public.usecase_skills') IS NOT NULL"),
    ("0002", "SELECT to_regclass('public.usecase_config') IS NOT NULL"),
    ("0001", "SELECT to_regclass('public.queries') IS NOT NULL"),
]


def _url() -> str:
    user = os.getenv("POSTGRES_USER", "rag")
    password = os.getenv("POSTGRES_PASSWORD", "changeme")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    database = os.getenv("POSTGRES_DB", "ragplatform")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


def _detect(engine) -> tuple[bool, str | None]:
    """Return (already stamped, detected revision or None for an empty DB)."""
    with engine.connect() as conn:
        stamped = conn.execute(
            text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
        ).scalar()
        if stamped:
            return True, None
        for revision, probe in _LEVELS:
            if conn.execute(text(probe)).scalar():
                return False, revision
    return False, None


def main() -> int:
    engine = create_engine(_url())
    config = Config("alembic.ini")

    try:
        stamped, revision = _detect(engine)
    except Exception as exc:  # noqa: BLE001
        print(f"Datenbank nicht erreichbar: {exc}", file=sys.stderr)
        return 1

    if stamped:
        print("Datenbank ist bereits unter Alembic-Verwaltung.")
    elif revision is None:
        print("Leere Datenbank — migriere von Grund auf.")
    else:
        # Stamp only: those revisions are already physically present, applying
        # them would fail on objects that exist.
        print(f"Datenbank stammt aus init.sql und entspricht Revision {revision}.")
        print(f"Setze Alembic-Marke auf {revision} (ohne sie auszuführen) ...")
        command.stamp(config, revision)

    print("Wende ausstehende Migrationen an ...")
    command.upgrade(config, "head")
    print("Fertig.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
