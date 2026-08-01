"""Background maintenance tasks for the SQLite database (WAL checkpointing)."""

import sqlite3
import threading

from config import DB_PATH
from core import SQLITE_BUSY_TIMEOUT_MS, logger

CHECKPOINT_INTERVAL_SECONDS = 5 * 60


def checkpoint_wal_once() -> None:
    """Checkpoint the WAL, truncating the log when no reader blocks it."""
    conn = sqlite3.connect(
        DB_PATH,
        timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
    )
    try:
        busy, total, done = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if busy:
            # A reader holds an open transaction, so TRUNCATE is all-or-nothing.
            # Fall back to a passive checkpoint up to the read barrier.
            row = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            logger.warning(
                "WAL truncate checkpoint blocked by %d reader(s); "
                "passive checkpointed %s/%s frames",
                busy, row[2], row[1],
            )
        elif done < total:
            logger.info("WAL checkpoint truncated %s/%s frames", done, total)
    except Exception as e:
        logger.error("WAL checkpoint failed: %s", e)
    finally:
        conn.close()


def _loop() -> None:
    while True:
        try:
            checkpoint_wal_once()
        except Exception:
            logger.error("WAL maintenance iteration failed", exc_info=True)
        threading.Event().wait(CHECKPOINT_INTERVAL_SECONDS)


def start_wal_maintenance() -> threading.Thread:
    """Start the daemon WAL checkpoint thread."""
    thread = threading.Thread(
        target=_loop,
        name="pymon-wal-maintenance",
        daemon=True,
    )
    thread.start()
    return thread
