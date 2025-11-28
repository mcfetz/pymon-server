from core import app  # and swagger if you need it elsewhere

# Import route modules so they can register their routes with `app`
from routes import alarms, metrics, plugins, agents


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
