import logging
import os
import threading

from flask import Flask, request
from flask_cors import CORS
from flasgger import Swagger
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from db_models import Base
from config import DB_PATH, CONF_DIR

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

# SQLAlchemy ORM Setup — absolute path so it works from any working directory
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
DATABASE_URL = f"sqlite:///{DB_PATH}"
SQLITE_BUSY_TIMEOUT_MS = 30_000

# SQLite still permits only one writer at a time. Serialize writes within the
# server process and let other processes wait instead of failing immediately.
DB_WRITE_LOCK = threading.RLock()

engine = create_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    connect_args={"timeout": SQLITE_BUSY_TIMEOUT_MS / 1000},
)


@event.listens_for(engine, "connect")
def _configure_sqlite(dbapi_connection, _connection_record):
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

# Create all tables if they do not exist yet
Base.metadata.create_all(bind=engine)

# Add columns that may be missing from existing databases (SQLite migration)
logger.info("Starting DB migration")
with engine.begin() as connection:
    existing_cols = {row[1] for row in connection.execute(text(
        "PRAGMA table_info(alarms)"
    ))}
    if 'acknowledged_at' not in existing_cols:
        logger.info("Migration: adding alarms.acknowledged_at")
        connection.execute(text("ALTER TABLE alarms ADD COLUMN acknowledged_at DATETIME"))
    if 'ack_method' not in existing_cols:
        logger.info("Migration: adding alarms.ack_method")
        connection.execute(text("ALTER TABLE alarms ADD COLUMN ack_method VARCHAR"))
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

logger.info("DB migration finished")

@app.after_request
def disable_api_caching(response):
    """Configuration and monitoring responses must always reflect current state."""
    if request.path.startswith(("/admin/", "/agents", "/groups", "/plugins", "/metrics", "/alarms")):
        response.headers["Cache-Control"] = "no-store, max-age=0"
    return response
