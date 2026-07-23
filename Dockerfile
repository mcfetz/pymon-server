FROM python:3.12-slim

# ── System dependencies ───────────────────────────────────────────────────
# gcc is needed by some psutil wheels on ARM; ping is used by the ping plugin
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        iputils-ping \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python dependencies ───────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application source ────────────────────────────────────────────────────
COPY *.py       ./
COPY routes/    routes/
COPY services/  services/
COPY plugins/   plugins/

# ── Persistent data ───────────────────────────────────────────────────────
# /data is the default DATA_DIR.  Mount a named volume here so that:
#   conf/        — agent config, rules, notifications, users, JWT secret
#   metrics.db   — SQLite database
# survive container restarts and image upgrades.
ENV PYMON_DATA_DIR=/data

RUN mkdir -p /data/conf

VOLUME ["/data"]

# ── Runtime ───────────────────────────────────────────────────────────────
EXPOSE 5000

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["python", "-u", "server.py"]
