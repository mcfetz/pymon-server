from core import app  # and swagger if you need it elsewhere

# Import route modules so they can register their routes with `app`
from routes import alarms, metrics, plugins, agents, push, admin
from routes.auth import login
from no_data_monitor import start_no_data_monitor
from db_maintenance import start_wal_maintenance
from cleanup_job import start_cleanup_job


if __name__ == "__main__":
    start_no_data_monitor()
    start_wal_maintenance()
    start_cleanup_job()
    app.run(debug=False, host="0.0.0.0", port=5000)
