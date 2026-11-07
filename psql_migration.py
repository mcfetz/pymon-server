"""One-shot migration of the SQLite database into PostgreSQL.

Triggered at startup when PYMON_MIGRATE_SQLITE_TO_PSQL=1 and
PYMON_DATABASE_URL points to a PostgreSQL database. Streams the large metrics
table with COPY, copies the smaller tables in id-ordered batches, repairs all
identity sequences afterwards and logs progress.
"""

import csv
import io
import os
import sqlite3
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import create_engine, inspect, text

from config import DATABASE_URL, DB_PATH, IS_POSTGRES
from core import logger

_BATCH_SIZE = int(os.environ.get("PYMON_MIGRATE_BATCH_SIZE", "500"))

# SQLite stores booleans as 0/1; PostgreSQL needs real booleans.
_BOOL_COLUMNS = {"alarms": {"acknowledged"}}


def _normalize_row(table: str, row: dict) -> dict:
    cast = _BOOL_COLUMNS.get(table, set())
    if not cast:
        return dict(row)
    return {
        k: (bool(v) if k in cast and v is not None else v)
        for k, v in row.items()
    }


def _columns(conn, table: str) -> list[str]:
    inspector = inspect(conn)
    return [c["name"] for c in inspector.get_columns(table)]


def _copy_batched(src_conn, dst_conn, table: str) -> int:
    """Copy a table with an integer 'id' pk in batches, returning row count."""
    columns = _columns(src_conn, table)
    placeholders = ", ".join(":" + c for c in columns)
    column_list = ", ".join(columns)
    copied = 0
    last_id = 0
    while True:
        if last_id == 0:
            rows = src_conn.execute(
                text(f"SELECT {column_list} FROM {table} ORDER BY id LIMIT :batch")
                .bindparams(batch=_BATCH_SIZE)
            ).mappings().all()
        else:
            rows = src_conn.execute(
                text(f"SELECT {column_list} FROM {table} WHERE id > :last_id ORDER BY id LIMIT :batch")
                .bindparams(last_id=last_id, batch=_BATCH_SIZE)
            ).mappings().all()
        if not rows:
            break
        with dst_conn.begin():
            dst_conn.execute(
                text(
                    f"INSERT INTO {table} ({column_list}) VALUES ({placeholders})"
                ),
                [_normalize_row(table, dict(r)) for r in rows],
            )
        copied += len(rows)
        last_id = rows[-1]["id"]
        logger.info("migrate: %s — %d rows copied", table, copied)
    return copied


def _copy_small(src_conn, dst_conn, table: str, where: str = "") -> int:
    columns = _columns(src_conn, table)
    column_list = ", ".join(columns)
    placeholders = ", ".join(":" + c for c in columns)
    query = f"SELECT {column_list} FROM {table}" + (f" WHERE {where}" if where else "")
    rows = src_conn.execute(text(query)).mappings().all()
    if not rows:
        return 0
    with dst_conn.begin():
        dst_conn.execute(
            text(f"INSERT INTO {table} ({column_list}) VALUES ({placeholders})"),
            [_normalize_row(table, dict(r)) for r in rows],
        )
    return len(rows)


_METRIC_COLUMNS = (
    "id", "agentid", "pluginid", "timestamp", "received_at",
    "metric", "value_float", "value_int", "value_str",
)
_METRIC_COPY_CHUNK = int(os.environ.get("PYMON_MIGRATE_COPY_CHUNK", "50000"))
# QUOTE_NOTNULL quotes an empty string but leaves None unquoted (COPY's NULL),
# so value_str='' survives the migration instead of turning into NULL.
_CSV_QUOTING = getattr(csv, "QUOTE_NOTNULL", csv.QUOTE_MINIMAL)


def _copy_metrics_fast(dst_conn) -> int:
    """Stream the metrics table from SQLite into PostgreSQL with COPY.

    The generic batched INSERT path is fine for alarms, notes and audit rows,
    but it is far too slow for the multi-million-row metrics table. COPY keeps
    the migration in the range of minutes instead of hours.
    """
    src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    total = 0
    try:
        cursor = src.execute(
            f"SELECT {', '.join(_METRIC_COLUMNS)} FROM metrics ORDER BY id"
        )
        raw_conn = dst_conn.connection.driver_connection
        while True:
            rows = cursor.fetchmany(_METRIC_COPY_CHUNK)
            if not rows:
                break
            buf = io.StringIO()
            writer = csv.writer(buf, lineterminator="\n", quoting=_CSV_QUOTING)
            for row in rows:
                writer.writerow([row[column] for column in _METRIC_COLUMNS])
            payload = io.BytesIO(buf.getvalue().encode("utf-8"))
            with raw_conn.cursor() as pg_cursor:
                pg_cursor.copy_expert(
                    f"COPY metrics ({', '.join(_METRIC_COLUMNS)}) "
                    "FROM STDIN WITH (FORMAT csv)",
                    payload,
                )
            total += len(rows)
            logger.info("migrate: metrics — %d rows copied (COPY)", total)
    finally:
        src.close()
    return total


def run_sqlite_to_psql_migration() -> None:
    """Entry point called at startup when the env switch is set."""
    if not IS_POSTGRES:
        logger.info("migrate: PYMON_MIGRATE_SQLITE_TO_PSQL set but target is SQLite — skipping")
        return
    if not os.path.exists(DB_PATH):
        logger.info("migrate: no SQLite database found at %s — skipping", DB_PATH)
        return

    logger.info("migrate: starting SQLite -> PostgreSQL migration from %s", DB_PATH)
    started = datetime.now()

    src_url = f"sqlite:///{DB_PATH}"
    src = create_engine(src_url)
    dst = create_engine(DATABASE_URL)

    # Schema must exist before copies; db_models defines portability-agnostic
    # column types, so build it through the ORM metadata.
    from db_models import Base
    with dst.begin():
        Base.metadata.create_all(bind=dst)

    # This is a one-shot copy, not a sync. Refuse to run against a target that
    # already contains metrics: a second run would collide on primary keys and
    # leave a half-copied database. Drop/recreate the target schema to re-run.
    with dst.connect() as conn:
        existing_metrics = conn.execute(text("SELECT COUNT(*) FROM metrics")).scalar() or 0
    if existing_metrics:
        logger.error(
            "migrate: target database already contains %d metrics — refusing to copy again",
            existing_metrics,
        )
        return

    try:
        with src.connect() as src_conn, dst.connect() as dst_conn:
            # Small reference tables first (no FK dependencies among them).
            n_push = _copy_small(src_conn, dst_conn, "push_subscriptions")
            n_last_seen = _copy_small(src_conn, dst_conn, "_metric_last_seen")
            logger.info("migrate: copied %d push subscriptions, %d last-seen rows", n_push, n_last_seen)

            # Bulk tables in dependency order: metrics before notes/alarms.
            n_metrics = 0
            fast_metrics = os.environ.get("PYMON_MIGRATE_FAST", "1") != "0"
            if fast_metrics:
                try:
                    with dst_conn.begin():
                        # The stats trigger updates one _db_stats row per metric.
                        # It is useful for live ingest but makes a bulk copy much
                        # slower; disable it for the copy and carry the source
                        # counter over explicitly below.
                        dst_conn.execute(text(
                            "ALTER TABLE metrics DISABLE TRIGGER trg_metrics_insert"
                        ))
                        n_metrics = _copy_metrics_fast(dst_conn)
                        dst_conn.execute(text(
                            "ALTER TABLE metrics ENABLE TRIGGER trg_metrics_insert"
                        ))
                except Exception:
                    logger.warning(
                        "migrate: COPY path failed; falling back to batched INSERT",
                        exc_info=True,
                    )
                    n_metrics = 0
            if not fast_metrics or n_metrics == 0:
                n_metrics = _copy_batched(src_conn, dst_conn, "metrics")
            n_notes = 0
            if inspect(src_conn).has_table("metric_notes"):
                n_notes = _copy_batched(src_conn, dst_conn, "metric_notes")
            n_alarms = _copy_small(
                src_conn, dst_conn, "alarms",
                where=("metrics_id IS NULL OR EXISTS "
                       "(SELECT 1 FROM metrics m WHERE m.id = alarms.metrics_id)"),
            )
            n_audit = 0
            if inspect(src_conn).has_table("audit_log"):
                n_audit = _copy_batched(src_conn, dst_conn, "audit_log")
            logger.info(
                "migrate: copied %d metric notes, %d alarms, %d audit rows",
                n_notes, n_alarms, n_audit,
            )
            with dst_conn.begin():
                target_metrics = dst_conn.execute(
                    text("SELECT COUNT(*) FROM metrics")
                ).scalar() or 0
            logger.info(
                "migrate: target metrics rows=%d (source copied=%d)",
                target_metrics, n_metrics,
            )
            if int(target_metrics) != int(n_metrics):
                logger.error(
                    "migrate: metric row-count mismatch (target=%d, copied=%d)",
                    target_metrics, n_metrics,
                )

            # Carry over the trigger-maintained metric counter.
            stats = src_conn.execute(text("SELECT * FROM _db_stats")).mappings().all()
            for row in stats:
                with dst_conn.begin():
                    dst_conn.execute(
                        text(
                            "INSERT INTO _db_stats (name, value) VALUES (:name, :value) "
                            "ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value"
                        ),
                        {"name": row["name"], "value": int(row["value"])},
                    )

            # Repair identity sequences so future inserts don't collide.
            # Every table with an auto-increment id must be repaired, not just
            # the large ones: an un-repaired sequence restarts at 1 and the
            # first new note/audit row would collide with a copied row.
            # Keep this in one explicit transaction; a bare connection close
            # would roll the setval() calls back.
            with dst_conn.begin():
                for table in (
                    "metrics", "alarms", "push_subscriptions",
                    "metric_notes", "audit_log",
                ):
                    seq = dst_conn.execute(
                        sa.text("SELECT pg_get_serial_sequence(:t, 'id')").bindparams(t=table)
                    ).scalar()
                    if not seq:
                        continue
                    dst_conn.execute(
                        sa.text(
                            "SELECT setval(:seq, COALESCE((SELECT MAX(id) FROM %s), 1))" % table
                        ).bindparams(seq=seq)
                    )
                    logger.info("migrate: sequence %s repaired", seq)
    finally:
        # Release the migration engines' FDs even on failure — they are not
        # the application's main engine.
        src.dispose()
        dst.dispose()

    logger.info(
        "migrate: SQLite -> PostgreSQL complete (metrics=%d, alarms=%d) in %.1fs",
        n_metrics, n_alarms,
        (datetime.now() - started).total_seconds(),
    )