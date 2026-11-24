import logging
import os
import re
import threading

from flask import Flask, request
from flask_cors import CORS
from flasgger import Swagger
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from db_models import Base
from config import DB_PATH, DATABASE_URL, IS_POSTGRES

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)

app = Flask(__name__)

# Restrict CORS to configured origins; override via PYMON_CORS_ORIGINS env var
# (comma-separated list, e.g. "https://pymon.example.com,http://localhost:5174")
_cors_origins = os.environ.get("PYMON_CORS_ORIGINS", "").strip()
CORS(app, origins=_cors_origins.split(",") if _cors_origins else [])

swagger = Swagger(app)

# SQLAlchemy ORM Setup — SQLite path absolute so it works from any working dir.
if not IS_POSTGRES:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
DATABASE_URL = DATABASE_URL
SQLITE_BUSY_TIMEOUT_MS = 30_000

# SQLite still permits only one writer at a time. Serialize writes within the
# server process and let other processes wait instead of failing immediately.
DB_WRITE_LOCK = threading.RLock()

engine = create_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    pool_pre_ping=True,
    connect_args={"timeout": SQLITE_BUSY_TIMEOUT_MS / 1000} if not IS_POSTGRES else {},
)


def _sqlite_regexp(pattern: str, value) -> int:
    """SQLite REGEXP implementation used by the metric search filter."""
    if value is None:
        return 0
    try:
        return 1 if re.search(pattern, str(value), re.IGNORECASE) else 0
    except re.error:
        # The API validates patterns before querying; keep SQLite from raising
        # if a malformed pattern ever reaches the database anyway.
        return 0


@event.listens_for(engine, "connect")
def _configure_sqlite(dbapi_connection, _connection_record):
    if IS_POSTGRES:
        return
    # SQLite ships no REGEXP implementation. Register one so metric queries can
    # use the same `column REGEXP pattern` expression as PostgreSQL.
    dbapi_connection.create_function("REGEXP", 2, _sqlite_regexp, deterministic=True)
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
        except Exception as exc:
            logger.warning("Unable to enable SQLite WAL mode: %s", exc)
        cursor.execute("PRAGMA synchronous=NORMAL")
        # Keep the WAL file bounded (~1 MB of uncheckpointed pages). Checkpoints
        # are skipped whenever any connection holds an open read transaction, so
        # db_maintenance also runs a periodic checkpoint on top of this.
        cursor.execute("PRAGMA wal_autocheckpoint=1000")
    finally:
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_db():
    """Create missing tables and bring the schema up to date.

    Must be called explicitly from the server start path (``server.py``).
    Importing this module must NOT run the migration on PostgreSQL: every
    process that imports ``core`` - for example an ad-hoc
    ``python3 -c "from rules import ..."`` inside the server container - would
    otherwise recreate the ``metrics`` triggers and take an AccessExclusiveLock
    on the hottest table, deadlocking a concurrent agent ingest.
    """
    # Create all tables if they do not exist yet
    Base.metadata.create_all(bind=engine)

    # Add columns that may be missing from existing databases (DB migration)
    logger.info("Starting DB migration")
    with engine.begin() as connection:
        if IS_POSTGRES:
            # A conflicting DDL statement must fail cleanly instead of waiting
            # forever for a lock on the hot metrics table (and deadlocking the
            # running ingest). Only ever relevant when DDL really runs, i.e. on
            # a schema change - the steady-state migration is a no-op.
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            _migrate_postgres(connection)
        else:
            _migrate_sqlite(connection)
    logger.info("DB migration finished")


def _migrate_postgres(connection):
    """PostgreSQL schema housekeeping (idempotent; runs inside one transaction)."""
    # ── PostgreSQL schema housekeeping ──
    existing_cols = {
        row[0]
        for row in connection.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'alarms'"
        ))
    }
    if 'acknowledged_at' not in existing_cols:
        logger.info("Migration: adding alarms.acknowledged_at")
        connection.execute(text("ALTER TABLE alarms ADD COLUMN acknowledged_at TIMESTAMP"))
    if 'ack_method' not in existing_cols:
        logger.info("Migration: adding alarms.ack_method")
        connection.execute(text("ALTER TABLE alarms ADD COLUMN ack_method VARCHAR"))
    if 'acknowledged_by' not in existing_cols:
        logger.info("Migration: adding alarms.acknowledged_by")
        connection.execute(text("ALTER TABLE alarms ADD COLUMN acknowledged_by VARCHAR"))
    if 'acknowledged_source' not in existing_cols:
        logger.info("Migration: adding alarms.acknowledged_source")
        connection.execute(text("ALTER TABLE alarms ADD COLUMN acknowledged_source VARCHAR"))
    if 'note' not in existing_cols:
        logger.info("Migration: adding alarms.note")
        connection.execute(text("ALTER TABLE alarms ADD COLUMN note TEXT"))
    if 'note_updated_at' not in existing_cols:
        logger.info("Migration: adding alarms.note_updated_at")
        connection.execute(text("ALTER TABLE alarms ADD COLUMN note_updated_at TIMESTAMP"))
    existing_metric_cols = {
        row[0]
        for row in connection.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'metrics'"
        ))
    }
    if 'received_at' not in existing_metric_cols:
        logger.info("Migration: adding metrics.received_at + backfill")
        connection.execute(text("ALTER TABLE metrics ADD COLUMN received_at TIMESTAMP"))
        connection.execute(text(
            "UPDATE metrics SET received_at = timestamp WHERE received_at IS NULL"
        ))
    value_int_type = connection.execute(text(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name = 'metrics' AND column_name = 'value_int'"
    )).scalar()
    if value_int_type == 'integer':
        logger.info("Migration: widening metrics.value_int to BIGINT")
        connection.execute(text(
            "ALTER TABLE metrics ALTER COLUMN value_int TYPE BIGINT"
        ))
    # topology_nodes was created by the first version of the topology
    # feature, which named a node by a single node_id and had no identifier
    # list. create_all() does not add columns to an existing table, so a
    # database that saw that version needs the column explicitly - without
    # it every topology query fails with UndefinedColumn.
    topology_cols = {
        row[0]
        for row in connection.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'topology_nodes'"
        ))
    }
    if topology_cols and 'ids' not in topology_cols:
        logger.info("Migration: adding topology_nodes.ids")
        connection.execute(text("ALTER TABLE topology_nodes ADD COLUMN ids TEXT"))

    # Indexes are created only when genuinely missing. `CREATE INDEX IF NOT
    # EXISTS` still resolves the table and would touch the hot metrics table on
    # every start; a plain check against the catalog takes no lock at all.
    existing_indexes = {
        row[0]
        for row in connection.execute(text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = current_schema() AND tablename = 'metrics'"
        ))
    }
    for index_name, ddl in (
        (
            "idx_metrics_plugin_metric_agent_received",
            "CREATE INDEX idx_metrics_plugin_metric_agent_received "
            "ON metrics (pluginid, metric, agentid, received_at)",
        ),
        (
            "idx_metrics_timestamp",
            "CREATE INDEX idx_metrics_timestamp ON metrics (timestamp)",
        ),
    ):
        if index_name not in existing_indexes:
            logger.info("Migration: creating index %s", index_name)
            connection.execute(text(ddl))

    connection.execute(text(
        "CREATE TABLE IF NOT EXISTS _db_stats (name TEXT PRIMARY KEY, value BIGINT NOT NULL DEFAULT 0)"
    ))
    if connection.execute(text(
        "SELECT 1 FROM _db_stats WHERE name = 'metrics'"
    )).scalar() is None:
        logger.info("Migration: initialising metric counter")
        connection.execute(text(
            "INSERT INTO _db_stats (name, value) SELECT 'metrics', COUNT(*) FROM metrics "
            "ON CONFLICT (name) DO NOTHING"
        ))
    connection.execute(text(
        "CREATE OR REPLACE FUNCTION pymon_stats_inc() RETURNS TRIGGER AS $$ "
        "BEGIN "
        "  INSERT INTO _db_stats(name, value) VALUES ('metrics', 1) "
        "  ON CONFLICT (name) DO UPDATE SET value = _db_stats.value + 1; "
        "  RETURN NEW; "
        "END; $$ LANGUAGE plpgsql"
    ))
    connection.execute(text(
        "CREATE OR REPLACE FUNCTION pymon_stats_dec() RETURNS TRIGGER AS $$ "
        "BEGIN "
        "  UPDATE _db_stats SET value = GREATEST(value - 1, 0) WHERE name = 'metrics'; "
        "  RETURN OLD; "
        "END; $$ LANGUAGE plpgsql"
    ))
    # Only create the triggers when they are missing. Recreating them on every
    # start (DROP + CREATE) takes an AccessExclusiveLock on `metrics`; a
    # concurrent agent INSERT then deadlocks against it and the whole payload is
    # lost (DeadlockDetected, HTTP 500). The trigger function bodies above are
    # refreshed via CREATE OR REPLACE, which keeps the trigger -> function link
    # intact and does not lock the table.
    existing_triggers = {
        row[0]
        for row in connection.execute(text(
            "SELECT t.tgname FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE c.relname = 'metrics' AND NOT t.tgisinternal"
        ))
    }
    if 'trg_metrics_insert' not in existing_triggers:
        logger.info("Migration: creating metrics trigger trg_metrics_insert")
        connection.execute(text(
            "CREATE TRIGGER trg_metrics_insert AFTER INSERT ON metrics "
            "FOR EACH ROW EXECUTE FUNCTION pymon_stats_inc()"
        ))
    if 'trg_metrics_delete' not in existing_triggers:
        logger.info("Migration: creating metrics trigger trg_metrics_delete")
        connection.execute(text(
            "CREATE TRIGGER trg_metrics_delete AFTER DELETE ON metrics "
            "FOR EACH ROW EXECUTE FUNCTION pymon_stats_dec()"
        ))
    connection.execute(text(
        "CREATE TABLE IF NOT EXISTS _metric_last_seen ("
        "agentid TEXT NOT NULL, "
        "pluginid TEXT NOT NULL, "
        "metric TEXT NOT NULL, "
        "last_received_at TIMESTAMP, "
        "PRIMARY KEY (agentid, pluginid, metric)"
        ")"
    ))


def _migrate_sqlite(connection):
    """SQLite schema housekeeping (idempotent; single-writer local file)."""
    # ── SQLite schema housekeeping ──
    existing_cols = {row[1] for row in connection.execute(text(
        "PRAGMA table_info(alarms)"
    ))}
    if 'acknowledged_at' not in existing_cols:
        logger.info("Migration: adding alarms.acknowledged_at")
        connection.execute(text("ALTER TABLE alarms ADD COLUMN acknowledged_at DATETIME"))
    if 'ack_method' not in existing_cols:
        logger.info("Migration: adding alarms.ack_method")
        connection.execute(text("ALTER TABLE alarms ADD COLUMN ack_method VARCHAR"))
    if 'acknowledged_by' not in existing_cols:
        logger.info("Migration: adding alarms.acknowledged_by")
        connection.execute(text("ALTER TABLE alarms ADD COLUMN acknowledged_by VARCHAR"))
    if 'acknowledged_source' not in existing_cols:
        logger.info("Migration: adding alarms.acknowledged_source")
        connection.execute(text("ALTER TABLE alarms ADD COLUMN acknowledged_source VARCHAR"))
    if 'note' not in existing_cols:
        logger.info("Migration: adding alarms.note")
        connection.execute(text("ALTER TABLE alarms ADD COLUMN note TEXT"))
    if 'note_updated_at' not in existing_cols:
        logger.info("Migration: adding alarms.note_updated_at")
        connection.execute(text("ALTER TABLE alarms ADD COLUMN note_updated_at DATETIME"))
    existing_metric_cols = {row[1] for row in connection.execute(text(
        "PRAGMA table_info(metrics)"
    ))}
    if 'received_at' not in existing_metric_cols:
        logger.info("Migration: adding metrics.received_at + backfill")
        connection.execute(text("ALTER TABLE metrics ADD COLUMN received_at DATETIME"))
        # Existing rows predate received_at; their metric timestamp is the best
        # available receive-time approximation for no-data rules.
        connection.execute(text(
            "UPDATE metrics SET received_at = timestamp WHERE received_at IS NULL"
        ))
    # Index only needed by no_data_monitor, not in the ORM model definition
    connection.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_metrics_plugin_metric_agent_received "
        "ON metrics (pluginid, metric, agentid, received_at)"
    ))
    # Time-range queries and the default DESC ordering need a timestamp-first
    # index; the composite indexes above cannot serve that access pattern.
    connection.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_metrics_timestamp ON metrics (timestamp)"
    ))
    # Persistent, trigger-maintained metric counter so maintenance stats
    # never have to scan the (potentially huge) metrics table.
    connection.execute(text(
        "CREATE TABLE IF NOT EXISTS _db_stats (name TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0)"
    ))
    if not connection.execute(text(
        "SELECT 1 FROM _db_stats WHERE name = 'metrics'"
    )).scalar():
        logger.info("Migration: initialising metric counter")
        connection.execute(text(
            "INSERT INTO _db_stats (name, value) SELECT 'metrics', COUNT(*) FROM metrics"
        ))
    connection.execute(text(
        "CREATE TRIGGER IF NOT EXISTS trg_metrics_insert AFTER INSERT ON metrics "
        "BEGIN UPDATE _db_stats SET value = value + 1 WHERE name = 'metrics'; END"
    ))
    connection.execute(text(
        "CREATE TRIGGER IF NOT EXISTS trg_metrics_delete AFTER DELETE ON metrics "
        "BEGIN UPDATE _db_stats SET value = value - 1 WHERE name = 'metrics'; END"
    ))
    # Per-metric "last received" tracker so no-data rules keep working even when
    # unchanged values are discarded at ingest (no Metrics row is written then).
    connection.execute(text(
        "CREATE TABLE IF NOT EXISTS _metric_last_seen ("
        "agentid TEXT NOT NULL, "
        "pluginid TEXT NOT NULL, "
        "metric TEXT NOT NULL, "
        "last_received_at DATETIME, "
        "PRIMARY KEY (agentid, pluginid, metric)"
        ")"
    ))


# On SQLite the database is a single local file: creating and migrating it on
# import is safe and keeps `import core` self-contained for the tests and for
# single-file installs. On PostgreSQL it must NOT happen on import - an
# AccessExclusiveLock on `metrics` from a foreign importer would deadlock a live
# agent ingest. There the server start path (server.py) calls init_db() instead.
if not IS_POSTGRES:
    init_db()


@app.after_request
def disable_api_caching(response):
    """Configuration and monitoring responses must always reflect current state."""
    if request.path.startswith(("/admin/", "/agents", "/groups", "/plugins", "/metrics", "/alarms", "/topology")):
        response.headers["Cache-Control"] = "no-store, max-age=0"
    return response
