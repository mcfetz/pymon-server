"""One-shot migration of the SQLite database into PostgreSQL.

Triggered at startup when PYMON_MIGRATE_SQLITE_TO_PSQL=1 and
PYMON_DATABASE_URL points to a PostgreSQL database. Copies every table in
id-ordered batches, repairs identity sequences afterwards and logs progress.
"""

import os
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

    try:
        with src.connect() as src_conn, dst.connect() as dst_conn:
            # Small reference tables first (no FK dependencies among them).
            n_push = _copy_small(src_conn, dst_conn, "push_subscriptions")
            n_last_seen = _copy_small(src_conn, dst_conn, "_metric_last_seen")
            logger.info("migrate: copied %d push subscriptions, %d last-seen rows", n_push, n_last_seen)

            # Bulk tables in dependency order: metrics before alarms.
            n_metrics = _copy_batched(src_conn, dst_conn, "metrics")
            n_alarms = _copy_small(
                src_conn, dst_conn, "alarms",
                where=("metrics_id IS NULL OR EXISTS "
                       "(SELECT 1 FROM metrics m WHERE m.id = alarms.metrics_id)"),
            )
            logger.info("migrate: copied %d alarms", n_alarms)

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
            for table in ("metrics", "alarms", "push_subscriptions"):
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