from core import app  # and swagger if you need it elsewhere

# Import route modules so they can register their routes with `app`
from routes import alarms, metrics, plugins, agents, push, admin, cron
from routes.auth import login
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
