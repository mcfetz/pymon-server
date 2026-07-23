"""
Central path configuration for pymon-server.

All persistent data lives under DATA_DIR so the application can be
containerised by pointing PYMON_DATA_DIR at a mounted volume:

    docker run -v /host/pymon-data:/data -e PYMON_DATA_DIR=/data pymon-server

In development (no env var set) DATA_DIR defaults to the server root so
nothing changes for existing setups.
"""

import os

# Absolute path to the directory containing this file (pymon-server/)
_SERVER_ROOT = os.path.dirname(os.path.abspath(__file__))

# ── Persistent data directory ─────────────────────────────────────────────
# Override with PYMON_DATA_DIR for Docker / production deployments.
DATA_DIR = os.path.abspath(os.environ.get("PYMON_DATA_DIR", _SERVER_ROOT))

# Derived paths
CONF_DIR    = os.path.join(DATA_DIR, "conf")
DB_PATH     = os.path.join(DATA_DIR, "metrics.db")

# Plugins directory (defaults to bundled plugins inside the server root).
# Override with PYMON_PLUGINS_DIR to use a custom/external plugins directory.
PLUGINS_DIR = os.path.abspath(
    os.environ.get("PYMON_PLUGINS_DIR", os.path.join(_SERVER_ROOT, "plugins"))
)
