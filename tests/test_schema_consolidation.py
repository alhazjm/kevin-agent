"""schema.sql must stay a faithful concatenation of supabase/migrations/.

supabase/schema.sql is the one-paste path for a fresh install; the numbered
migrations are the source of truth. Those two only stay honest if something
fails loudly when they drift — otherwise adding 0008 and forgetting to
regenerate ships a schema.sql that silently omits a table, and the first
person to use it gets a half-built database with no error.

Same idea as the idempotency parity pins and the Dockerfile marker greps:
duplication is allowed here, but only when a test refuses to let it rot.
"""

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SUPABASE_DIR = REPO_ROOT / "supabase"


def _load_builder():
    """Import supabase/build_schema.py by path.

    supabase/ is deliberately not a Python package (it is SQL that humans
    paste into a dashboard), so there is no __init__.py to import through.
    """
    spec = importlib.util.spec_from_file_location(
        "build_schema", SUPABASE_DIR / "build_schema.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSchemaConsolidation:
    def test_schema_sql_exists(self):
        assert (SUPABASE_DIR / "schema.sql").exists(), (
            "supabase/schema.sql is missing — run: python supabase/build_schema.py"
        )

    def test_schema_sql_matches_migrations(self):
        """The committed file equals what the generator produces right now."""
        builder = _load_builder()
        committed = builder._read(SUPABASE_DIR / "schema.sql")
        expected = builder.build()
        assert committed == expected, (
            "supabase/schema.sql is stale. A migration was added or edited "
            "without regenerating it. Run: python supabase/build_schema.py"
        )

    def test_every_migration_is_included(self):
        """Belt and braces: each migration's filename appears as a section."""
        builder = _load_builder()
        schema = builder._read(SUPABASE_DIR / "schema.sql")
        files = builder.migration_files()
        assert files, "no migrations discovered"
        for f in files:
            assert f.name in schema, f"{f.name} missing from schema.sql"

    def test_migrations_are_contiguously_numbered(self):
        """0001..000N with no gaps and no duplicate prefixes.

        A gap usually means a migration was renamed or dropped, which breaks
        the promise that running them in order reproduces a live database.
        """
        builder = _load_builder()
        numbers = [int(f.name[:4]) for f in builder.migration_files()]
        assert numbers == list(range(1, len(numbers) + 1)), (
            f"migration numbering is not contiguous: {numbers}"
        )

    def test_schema_carries_the_is_owner_warning(self):
        """The one edit a new installer MUST make before running it."""
        builder = _load_builder()
        schema = builder._read(SUPABASE_DIR / "schema.sql")
        assert "is_owner()" in schema
        assert "GENERATED FILE" in schema

    def test_no_psql_meta_commands(self):
        """Pasting into the Supabase SQL editor means no \\ commands.

        The editor is not psql; a backslash command would be a syntax error
        halfway through a paste that has already created tables.
        """
        builder = _load_builder()
        for f in builder.migration_files():
            for i, line in enumerate(builder._read(f).splitlines(), 1):
                assert not line.lstrip().startswith("\\"), (
                    f"{f.name}:{i} uses a psql meta-command, which breaks the "
                    "single-paste path"
                )

    def test_no_concurrent_index_builds(self):
        """CREATE INDEX CONCURRENTLY cannot run inside a transaction block.

        The whole point of the one-paste path is that a failure rolls back
        rather than leaving a half-built schema, so nothing may opt out of
        the surrounding transaction.
        """
        builder = _load_builder()
        for f in builder.migration_files():
            assert "concurrently" not in builder._read(f).lower(), (
                f"{f.name} uses CONCURRENTLY, which cannot run in a "
                "transaction and breaks the single-paste path"
            )
