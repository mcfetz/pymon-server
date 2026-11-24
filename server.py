from config import MIGRATE_SQLITE_TO_PSQL

# One-shot SQLite -> PostgreSQL migration. Must run before core/ORM touches
# the target database with real data.
if MIGRATE_SQLITE_TO_PSQL:
    from psql_migration import run_sqlite_to_psql_migration
    run_sqlite_to_psql_migration()

from core import app, init_db  # and swagger if you need it elsewhere

# Schema creation and migration run here, in the server start path - not on
# `import core`. On PostgreSQL an import-time migration would take an
# AccessExclusiveLock on the hot `metrics` table (DROP/CREATE TRIGGER) and
# deadlock a concurrent agent ingest. Must run before any route or worker
# thread touches the database.
init_db()

# Import route modules so they can register their routes with `app`
from routes import alarms, metrics, plugins, agents, push, admin, cron, users, audit, topology  # noqa: F401
from routes.auth import login  # noqa: F401
from no_data_monitor import start_no_data_monitor
from db_maintenance import start_wal_maintenance
from cleanup_job import start_cleanup_job
from cron_scheduler import start_cron_scheduler


if __name__ == "__main__":
    start_no_data_monitor()
    start_wal_maintenance()
    start_cleanup_job()
    start_cron_scheduler()
    app.run(debug=False, host="0.0.0.0", port=5000)
