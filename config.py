"""
Central path configuration for pymon-server.

All persistent data lives under DATA_DIR so the application can be
containerised by pointing PYMON_DATA_DIR at a mounted volume:

    docker run -v /host/pymon-data:/data -e PYMON_DATA_DIR=/data pymon-server

In development (no env var set) DATA_DIR defaults to the server root so
nothing changes for existing setups.
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

# Absolute path to the directory containing this file (pymon-server/)
_SERVER_ROOT = os.path.dirname(os.path.abspath(__file__))

# ── Persistent data directory ─────────────────────────────────────────────
# Override with PYMON_DATA_DIR for Docker / production deployments.
DATA_DIR = os.path.abspath(os.environ.get("PYMON_DATA_DIR", _SERVER_ROOT))

# Derived paths
CONF_DIR       = os.path.join(DATA_DIR, "conf")
DB_PATH        = os.path.join(DATA_DIR, "metrics.db")

# ── Database ───────────────────────────────────────────────────────────────
# PostgreSQL connection override. When set (e.g.
# PYMON_DATABASE_URL=postgresql://pymon:pass@pymon-db:5432/pymon) the server
# uses PostgreSQL instead of the default SQLite file.
DATABASE_URL = os.environ.get("PYMON_DATABASE_URL", "").strip() or f"sqlite:///{DB_PATH}"
IS_POSTGRES = DATABASE_URL.startswith("postgresql")
# Set to "1" to migrate the existing SQLite database into PostgreSQL at startup.
MIGRATE_SQLITE_TO_PSQL = os.environ.get("PYMON_MIGRATE_SQLITE_TO_PSQL", "").strip() == "1"

# Plugins directory (defaults to bundled plugins inside the server root).
# Override with PYMON_PLUGINS_DIR to use a custom/external plugins directory.
PLUGINS_DIR = os.path.abspath(
    os.environ.get("PYMON_PLUGINS_DIR", os.path.join(_SERVER_ROOT, "plugins"))
)

# ── Local timezone ────────────────────────────────────────────────────────
# Cron schedules and the daily cleanup time are interpreted in this
# timezone. Override with PYMON_TZ (e.g. "Europe/Berlin"); default is the
# host/container configured local time (respects the TZ env var).
try:
    LOCAL_TZ: object = ZoneInfo(os.environ.get("PYMON_TZ", "").strip()) if os.environ.get("PYMON_TZ", "").strip() else datetime.now().astimezone().tzinfo
except Exception:
    LOCAL_TZ = datetime.now().astimezone().tzinfo
