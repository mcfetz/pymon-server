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
    finally:
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

# Create all tables if they do not exist yet
Base.metadata.create_all(bind=engine)

# Add columns that may be missing from existing databases (SQLite migration)
with engine.begin() as connection:
    existing_cols = {row[1] for row in connection.execute(text(
        "PRAGMA table_info(alarms)"
    ))}
    if 'acknowledged_at' not in existing_cols:
        connection.execute(text("ALTER TABLE alarms ADD COLUMN acknowledged_at DATETIME"))
    if 'ack_method' not in existing_cols:
        connection.execute(text("ALTER TABLE alarms ADD COLUMN ack_method VARCHAR"))

@app.after_request
def disable_api_caching(response):
    """Configuration and monitoring responses must always reflect current state."""
    if request.path.startswith(("/admin/", "/agents", "/groups", "/plugins", "/metrics", "/alarms")):
        response.headers["Cache-Control"] = "no-store, max-age=0"
    return response
