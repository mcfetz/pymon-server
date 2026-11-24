"""`import core` must not touch the schema on PostgreSQL.

Incident this guards against (2026-11-24): every process that imported `core`
unconditionally ran `Base.metadata.create_all` plus the migration, including
`DROP TRIGGER IF EXISTS trg_metrics_insert ON metrics` + `CREATE TRIGGER`. That
takes an AccessExclusiveLock on the hottest table. An ad-hoc
`docker exec ... python3 -c "from rules import ..."` inside the server container
therefore deadlocked a concurrent agent ingest: PostgreSQL killed one side and
the whole payload was lost (`psycopg2.errors.DeadlockDetected`, HTTP 500).

Contract tested here:
1. Importing `core` with a PostgreSQL URL performs no schema DDL at all - it
   must not even open a database connection. `init_db()` moved to the server
   start path (`server.py`).
2. Importing `core` with the default SQLite backend still creates and migrates
   the local database, so the tests and single-file installs keep working.
3. `init_db()` is idempotent: running it twice creates each trigger exactly
   once and leaves the schema unchanged.

Run with:  venv/bin/python tests/test_core_import_safety.py
"""

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(TESTS_DIR)

TMP = tempfile.mkdtemp(prefix="pymon_core_import_")

RESULTS = []


def check(name, got, want):
    ok = got == want
    RESULTS.append((name, ok, got, want))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got={got!r} want={want!r}")


def check_true(name, cond, detail=""):
    check(f"{name} {detail}".strip(), bool(cond), True)


def _run(code, data_dir, db_url=None, timeout=120):
    env = os.environ.copy()
    env["PYMON_DATA_DIR"] = data_dir
    if db_url is None:
        env.pop("PYMON_DATABASE_URL", None)
    else:
        env["PYMON_DATABASE_URL"] = db_url
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc


def case_import_postgres_runs_no_migration():
    """A PostgreSQL import must not migrate - and must not connect at all.

    The URL points at 127.0.0.1:1 (nothing listens there). If the import tried to
    reach the database, the run would fail; a clean exit proves no connection was
    opened. Any migration would additionally log 'Starting DB migration'.
    """
    data_dir = tempfile.mkdtemp(prefix="pymon_import_pg_", dir=TMP)
    proc = _run(
        "import core; print('IMPORT_OK', callable(core.init_db))",
        data_dir,
        db_url="postgresql://pymon:secret@127.0.0.1:1/pymon",
    )
    check_true(
        "postgres: import core exits cleanly",
        proc.returncode == 0,
        f"(rc={proc.returncode}, stderr={proc.stderr.strip()[-300:]})",
    )
    check_true(
        "postgres: init_db is exposed",
        "IMPORT_OK True" in proc.stdout,
        f"(stdout={proc.stdout.strip()!r})",
    )
    check_true(
        "postgres: no migration on import",
        "Starting DB migration" not in proc.stderr,
        f"(stderr={proc.stderr.strip()[-300:]})",
    )
    check_true(
        "postgres: no migration finished marker",
        "DB migration finished" not in proc.stderr,
        f"(stderr={proc.stderr.strip()[-300:]})",
    )


def case_import_sqlite_still_migrates():
    """The SQLite path keeps creating the schema on import (tests rely on it)."""
    data_dir = tempfile.mkdtemp(prefix="pymon_import_sqlite_", dir=TMP)
    proc = _run("import core; print('IMPORT_OK')", data_dir)
    check_true(
        "sqlite: import core exits cleanly",
        proc.returncode == 0,
        f"(rc={proc.returncode}, stderr={proc.stderr.strip()[-300:]})",
    )
    check_true(
        "sqlite: migration ran on import",
        "Starting DB migration" in proc.stderr,
        f"(stderr={proc.stderr.strip()[-300:]})",
    )

    db_path = os.path.join(data_dir, "metrics.db")
    check_true("sqlite: database file created", os.path.exists(db_path), db_path)
    if not os.path.exists(db_path):
        return
    con = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        triggers = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
    finally:
        con.close()
    check_true("sqlite: metrics table exists", "metrics" in tables, str(sorted(tables)))
    check_true(
        "sqlite: _db_stats table exists", "_db_stats" in tables, str(sorted(tables))
    )
    check_true(
        "sqlite: both stats triggers exist",
        {"trg_metrics_insert", "trg_metrics_delete"} <= triggers,
        str(sorted(triggers)),
    )


def case_init_db_is_idempotent():
    """Calling init_db() again must not duplicate triggers or fail."""
    data_dir = tempfile.mkdtemp(prefix="pymon_import_idem_", dir=TMP)
    proc = _run(
        "import core\ncore.init_db()\ncore.init_db()\nprint('INIT_OK')\n",
        data_dir,
    )
    check_true(
        "idempotent: repeated init_db exits cleanly",
        proc.returncode == 0,
        f"(rc={proc.returncode}, stderr={proc.stderr.strip()[-300:]})",
    )
    check_true(
        "idempotent: marker printed",
        "INIT_OK" in proc.stdout,
        f"(stdout={proc.stdout.strip()!r})",
    )

    db_path = os.path.join(data_dir, "metrics.db")
    if not os.path.exists(db_path):
        check_true("idempotent: database file exists", False, db_path)
        return
    con = sqlite3.connect(db_path)
    try:
        insert_triggers = con.execute(
            "SELECT count(*) FROM sqlite_master "
            "WHERE type='trigger' AND name='trg_metrics_insert'"
        ).fetchone()[0]
    finally:
        con.close()
    check("idempotent: trigger created exactly once", insert_triggers, 1)


def main():
    print("core import safety tests")
    case_import_postgres_runs_no_migration()
    case_import_sqlite_still_migrates()
    case_init_db_is_idempotent()

    print()
    failed = [r for r in RESULTS if not r[1]]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} bestanden")
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
