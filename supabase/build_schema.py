#!/usr/bin/env python3
"""Regenerate supabase/schema.sql from supabase/migrations/*.sql.

Why this exists: a first-time installer wants ONE paste into the Supabase
SQL editor, not seven. But the numbered migrations have to stay the source
of truth — an existing database has already run them individually, so every
future schema change is a new numbered file, and squashing them would fork
this repo's schema from any database already built from it.

So schema.sql is DERIVED, never authored. `tests/test_schema_consolidation.py`
imports `build()` from this module and asserts the committed file matches,
which means adding 0008 without regenerating fails the suite instead of
silently shipping a schema.sql that is missing a table.

Usage:  python supabase/build_schema.py          # rewrite schema.sql
        python supabase/build_schema.py --check  # exit 1 if stale
"""

from __future__ import annotations

import sys
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

_RULE = "-- " + "=" * 73


def migration_files() -> list[Path]:
    """Numbered migrations in run order.

    Lexicographic sort IS numeric order while the prefix stays zero-padded
    to four digits, which the naming convention guarantees.
    """
    return sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))


def _read(path: Path) -> str:
    """Read as text with newlines normalised to \\n.

    The repo is edited on Windows, so files land in the working tree with
    CRLF. Normalising here keeps build() byte-identical on either platform —
    without it the parity test passes on one machine and fails on the other.
    """
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")


def build() -> str:
    """The full contents of schema.sql, as a string."""
    files = migration_files()
    if not files:
        raise SystemExit(f"no migrations found in {MIGRATIONS_DIR}")

    listing = "\n".join(f"--   {f.name}" for f in files)
    header = f"""{_RULE}
-- Kevin — consolidated database schema
--
-- GENERATED FILE — do not edit by hand.
-- Regenerate with:  python supabase/build_schema.py
--
-- Every file in supabase/migrations/ concatenated in run order, so a fresh
-- install is one paste into the Supabase SQL editor instead of {len(files)}.
--
-- The numbered migrations remain the source of truth. A database that
-- already exists has run them one at a time, so a new schema change is
-- always a NEW numbered file — never an edit to this one, and never a
-- re-squash. Regenerate this file in the same commit.
--
-- Every statement is idempotent (create table if not exists, add column if
-- not exists, and the do $$ ... duplicate_object policy pattern), so
-- running this more than once is safe.
--
-- BEFORE YOU RUN IT: change the email address in is_owner() to the one you
-- will sign into the dashboard with. Every row-level-security policy calls
-- that function, so leaving it wrong means the dashboard signs in and shows
-- you nothing, with no error.
--
-- Source files, in order:
{listing}
{_RULE}
"""

    parts = [header]
    for f in files:
        body = _read(f).strip("\n")
        parts.append(f"\n{_RULE}\n-- {f.name}\n{_RULE}\n\n{body}\n")
    return "".join(parts)


def main(argv: list[str]) -> int:
    content = build()
    if "--check" in argv:
        if not SCHEMA_PATH.exists():
            print("schema.sql is missing — run: python supabase/build_schema.py")
            return 1
        if _read(SCHEMA_PATH) != content:
            print("schema.sql is stale — run: python supabase/build_schema.py")
            return 1
        print("schema.sql is up to date")
        return 0

    SCHEMA_PATH.write_text(content, encoding="utf-8", newline="\n")
    print(f"wrote {SCHEMA_PATH.name} from {len(migration_files())} migrations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
