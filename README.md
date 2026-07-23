# pymon-server

Central monitoring server for the **pymon** ecosystem.

Agents download plugins from this server, run them locally, and push metrics back.
The server evaluates alarm rules, sends notifications, and runs shell executors.

## Architecture

```
                    ┌──────────────────────┐
                    │      pymon-server    │
                    │   Flask + SQLite     │
                    │   port 5000          │
                    └──┬───────────────┬───┘
                  ┌────┘               └──────┐
          POST /push/<agentid>        GET /plugins/<name>
          (metrics + results)         (plugin source)
                  │                           │
          ┌───────▼──────────┐    ┌───────────▼────────┐
          │  pymon-agent     │    │  pymon-agent       │
          │  (N instances)   │    │  downloads plugin  │
          │  runs plugins,   │    │  source at startup │
          │  pushes metrics  │    └────────────────────┘
          └──────────────────┘
```

Each agent runs its assigned plugins in threads, collects metrics, and pushes them
to the server. The server stores metrics in SQLite, evaluates rules, and triggers
alarms, notifications, and executors.

## Quick Start

```bash
cd pymon-server
pip install -r requirements.txt

# Required: set allowed frontend origin before starting
export PYMON_CORS_ORIGINS=http://localhost:5174

python server.py
```

Server starts on `http://0.0.0.0:5000`. Swagger UI at `http://localhost:5000/apidocs`.

Default login: `admin` / `admin` (change immediately via the Account page).

## Configuration

All configuration lives in the `conf/` directory as JSON files.
Files are created automatically on first write if they do not exist.

| File | Contents |
|------|----------|
| `conf/agents.json` | Agents, groups, API keys, plugin assignments |
| `conf/rules.json` | Alarm rules |
| `conf/executors.json` | Shell executors |
| `conf/notifications.json` | Notification targets |
| `conf/blackouts.json` | Blackout windows |
| `conf/plugins.json` | Plugin metadata overrides (label, description, enabled) |
| `conf/users.json` | Frontend user accounts (hashed passwords) |
| `conf/jwt_secret.txt` | JWT signing secret (auto-generated on first start) |
| `conf/snoozes.json` | Active alarm snoozes (runtime state) |

### Agents (`conf/agents.json`)

```json
{
  "agents": {
    "macbook": {
      "title": "macbook.lan",
      "description": "Macbook agent",
      "apikey": "abc123",
      "enabled": true,
      "groups": ["default", "ping"],
      "plugins": {
        "cpu":  { "sleep": 30 },
        "ping": { "sleep": 60, "hosts": ["8.8.8.8"] }
      }
    }
  },
  "groups": {
    "default": {
      "title": "Default",
      "description": "Standard plugin set",
      "plugins": ["host", "cpu", "ram", "disk_usage", "network"]
    }
  }
}
```

- The agent's JSON key is its ID (used in all cross-references).
- `groups` in agent config reference group IDs.
- `plugins` in group config reference plugin names (filename without `.py`).

### Rules (`conf/rules.json`)

```json
{
  "cpu_high": {
    "id": "cpu_high",
    "enabled": true,
    "description": "CPU > 80%",
    "pluginid": "cpu",
    "metric": "percent",
    "condition": "gt",
    "threshold": 80.0,
    "scope": "single",
    "severity": "warning",
    "fire": "single",
    "notifications": ["noglz03s"],
    "executors": [],
    "agents": [],
    "agents_mode": "exclude"
  }
}
```

**Scopes**

| Scope | Behaviour |
|-------|-----------|
| `single` | Evaluate each individual measurement |
| `moving_avg` | Average of the last `window_size` measurements |
| `count_ratio` | Number of the last `window_size` measurements that violate the threshold; fires when ≥ `min_violations` |

**Conditions:** `gt`, `ge`, `lt`, `le`, `eq`, `ne`

**Fire modes**

| Mode | Behaviour |
|------|-----------|
| `single` | At most one open alarm per (agent, rule) |
| `multi` | New alarm on every violation |
| `replace` | Acknowledge all open alarms for this combo, then create one new alarm |

**Agent filter**

- `agents_mode: "exclude"` + empty `agents` → rule applies to all agents (default)
- `agents_mode: "exclude"` + list → rule skips those agents
- `agents_mode: "include"` + list → rule only applies to those agents

### Notifications (`conf/notifications.json`)

Supported types: `email`, `ntfy`, `web_push`, `twilio_call`.

```json
{
  "my_ntfy": {
    "id": "my_ntfy",
    "enabled": true,
    "type": "ntfy",
    "ntfy_url": "https://ntfy.sh",
    "ntfy_topic": "my-alerts",
    "ntfy_access_token": ""
  }
}
```

For `email`, set the SMTP password via the `NOTIFY_EMAIL_PASSWORD` environment
variable rather than storing it in the config file.

### Executors (`conf/executors.json`)

```json
{
  "log_alarm": {
    "id": "log_alarm",
    "enabled": true,
    "title": "Log alarm",
    "command": "echo '[{severity}] {rule_id} on {agentid}: {metric}={value}' >> /tmp/alarms.log",
    "execution_target": "server"
  }
}
```

`execution_target` is `"server"` (runs on the server) or `"agent"` (sent to the
agent to run locally).

Template variables: `{rule_id}`, `{agentid}`, `{pluginid}`, `{metric}`, `{value}`,
`{message}`, `{severity}`.

### Blackouts (`conf/blackouts.json`)

Suppress alarms or notifications during scheduled maintenance windows.

```json
{
  "nightly": {
    "id": "nightly",
    "enabled": true,
    "title": "Nightly maintenance",
    "weekdays": [0, 1, 2, 3, 4],
    "start_time": "02:00",
    "end_time": "04:00",
    "target_rules": [],
    "target_agents": [],
    "target_groups": ["default"],
    "mode": "no_alarms"
  }
}
```

- `weekdays`: 0 = Monday … 6 = Sunday
- Overnight windows (e.g. `22:00`–`06:00`) are supported
- `mode`: `"no_alarms"` (suppress alarm creation) or `"no_notifications"` (create alarm, skip notifications)
- Empty `target_*` arrays with mode `no_alarms` = global blackout (blocks everything)

## Authentication

### Frontend users (JWT)

The web frontend authenticates with username/password via `POST /login`, receives
a JWT (30-day TTL), and sends it as `Authorization: Bearer <token>` on every request.

User accounts are stored in `conf/users.json` with scrypt-hashed passwords.
Manage accounts via the Account page in the web UI.

### Agents (API key)

Agents authenticate using two headers:

```
agentid: macbook
X-API-Key: abc123
```

API keys are stored in `conf/agents.json`. Generate a secure key with:

```bash
python -c "import secrets; print(secrets.token_hex(16))"
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PYMON_CORS_ORIGINS` | *(none — blocks all cross-origin requests)* | Comma-separated list of allowed frontend origins |
| `NOTIFY_EMAIL_PASSWORD` | — | SMTP password for email notifications |

### CORS

By default the server allows **no cross-origin requests**.
Set `PYMON_CORS_ORIGINS` before starting the server:

```bash
# Development
export PYMON_CORS_ORIGINS=http://localhost:5174

# Production
export PYMON_CORS_ORIGINS=https://pymon.example.com

# Multiple origins
export PYMON_CORS_ORIGINS=https://pymon.example.com,https://staging.example.com

python server.py
```

> If the frontend shows network errors immediately after login, a missing or wrong
> `PYMON_CORS_ORIGINS` value is the most likely cause.

## Plugin System

Plugins are Python scripts stored in the `plugins/` directory.
Agents download them at startup and run them in threads.

Each plugin must expose:
- `__schema__` — dict describing the plugin (label, description, fields)
- A `run(config)` function that returns a flat `dict[str, float | str]` of metric values

Plugins can be created, edited, duplicated, and deleted via the web UI.
The `plugins/_template.py` file provides a starting skeleton.

### Built-in Plugins

| Plugin | Key metrics |
|--------|-------------|
| `cpu` | `percent` |
| `ram` | `virtual_pct`, `swap_pct` |
| `disk_usage` | `/<mountpoint>` (percent per partition) |
| `network` | `<iface>:bytes_sent`, `<iface>:bytes_recv`, `<iface>:tx_bytes_per_sec`, `<iface>:rx_bytes_per_sec`, `tcp_open_connections` |
| `host` | `hostname`, `uptime`, `os`, `os_version`, `total_ram`, `cpu_count`, `cpu_model`, `ip:*` |
| `ping` | `<host> success`, `<host> avg-time` |
| `http_check` | `<name>:status_code`, `<name>:content_ok` |
| `services` | `<service>` (string: `running` / `stopped` / `failed`) |
| `temperature` | `<sensor>:<label>` (°C) |
| `cert_valid` | `<url>` (days until certificate expiry) |
| `docker_host` | `containers_running`, `containers_stopped`, `images_total`, `volumes_total` |

## API Endpoints

### Auth

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/login` | — | Obtain JWT token |
| PUT | `/account` | JWT | Change username / password |

### Agents & Groups

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/agents` | agent/JWT | List agents with online status |
| GET | `/groups` | agent/JWT | List groups with agent memberships |
| GET | `/agents/<id>/plugins` | agent/JWT | Plugin configs for an agent |
| GET | `/agents/<id>/plugins/<p>/metrics` | agent/JWT | Metric names for agent+plugin |

### Metrics

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/push/<agentid>` | agent | Ingest metrics from an agent |
| GET | `/metrics/query` | agent/JWT | Query stored metrics (time range, agent, plugin, metric filters) |

### Alarms

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/alarms` | agent/JWT | List alarms (filters: `acknowledged`, `agentid`, `limit`) |
| GET | `/alarms/open` | agent/JWT | List open (unacknowledged) alarms |
| GET | `/alarms/<id>/ack` | agent/JWT | Acknowledge an alarm |
| GET | `/alarms/snoozed` | agent/JWT | List active snoozes |
| POST | `/alarms/snooze/toggle` | agent/JWT | Toggle snooze for a rule+agent+metric combo |

### Admin (Config CRUD)

All admin routes require authentication. Prefix: `/admin/`

| Resource | Methods |
|----------|---------|
| `/admin/agents` | GET, POST |
| `/admin/agents/<id>` | PUT, DELETE |
| `/admin/agents/<id>/enabled` | PUT |
| `/admin/agents/<id>/groups` | PUT |
| `/admin/agents/<id>/plugins/<p>` | PUT, DELETE |
| `/admin/groups` | GET |
| `/admin/groups/<id>` | PUT, DELETE |
| `/admin/rules` | GET |
| `/admin/rules/<id>` | PUT, DELETE |
| `/admin/executors` | GET |
| `/admin/executors/<id>` | PUT, DELETE |
| `/admin/notifications` | GET |
| `/admin/notifications/<id>` | PUT, DELETE |
| `/admin/notifications/test` | POST |
| `/admin/blackouts` | GET |
| `/admin/blackouts/<id>` | PUT, DELETE |
| `/admin/plugins` | GET |
| `/admin/plugins/<name>/source` | GET, PUT |
| `/admin/plugins/<name>` | DELETE |
| `/admin/plugins/check` | POST (syntax check) |

## Database

SQLite file `metrics.db` is created automatically.

| Table | Contents |
|-------|----------|
| `metrics` | All metric data points (agentid, pluginid, metric, timestamp, value) |
| `alarms` | Triggered alarms with severity, ack status, link to triggering metric |

## Security

- Frontend authentication uses JWT (HS256, 30-day TTL)
- Agent authentication uses `agentid` + `X-API-Key` with constant-time comparison
- CORS is restricted to configured origins only (see `PYMON_CORS_ORIGINS`)
- Login endpoint is rate-limited: max 10 attempts per IP per 60 seconds
- Plugin filenames and paths are validated against a strict allowlist before any file I/O
- Shell executor commands run with `shell=False` (`shlex.split`) to prevent injection
- `ntfy_url` is validated against private/loopback IP ranges before outbound requests
- All JSON config writes are atomic (temp file + `os.replace`) and protected by per-file threading locks
- SMTP password should be set via `NOTIFY_EMAIL_PASSWORD` env var, not stored in config
