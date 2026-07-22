# pymon-server

Central monitoring server. Part of the **pymon** ecosystem (server + agent + CLI).

Agents download plugins from this server, run them locally, and push metrics back. The server evaluates alarm rules, sends email notifications, and runs shell executors.

## Architecture

```
                    ┌─────────────────┐
                    │   pymon-server  │
                    │  Flask + SQLite │
                    │  port 5000      │
                    └──┬──────────┬───┘
                  ┌────┘          └──────┐
          POST /metrics             GET /plugins
          GET /plugins/<name>/config  GET /plugins/<name>
                  │                          │
          ┌───────▼──────────┐    ┌──────────▼──────────┐
          │  pymon-agent     │    │  pymon-agent        │
          │  (N instances)   │    │  downloads plugin   │
          │  pushes metrics  │    │  source + config    │
          └──────────────────┘    └─────────────────────┘
```

Each agent runs its assigned plugins in separate threads, collects metrics, and sends them to the server via a queue. The server stores metrics in SQLite, evaluates rules, and triggers alarms/notifications/executors.

## Quick Start

```bash
cd pymon-server
pip install -r requirements.txt
python server.py
```

Server starts on `http://0.0.0.0:5000`. Swagger UI at `http://localhost:5000/apidocs`.

## Configuration

All configuration is in the `conf/` directory as TOML files.

### `conf/config.toml` — Agent-to-Group and Group-to-Plugin assignment

```toml
[agents]
agent1 = ["default", "ping"]
agent2 = ["docker"]

[groups]
default = ["plugin_base", "host", "cpu", "ram", "disk_usage", "cert_valid", "temperature", "network", "services", "http"]
docker = ["docker_host"]
ping = ["ping"]
```

### `conf/agents.toml` — Per-agent plugin configuration

```toml
[agent1]
  [agent1.cpu]
  sleep = 30

  [agent1.ping]
  sleep = 60
  hosts = ["google.de", "example.com"]
```

Each top-level key is an agent ID. Under it, each key is a plugin name with its configuration.

### `conf/apikeys.toml` — API keys

```toml
[agent1]
key = "111"
type = "agent"
description = "Default API key for agent1"
```

Three types: `agent` (for agents pushing metrics), `user` (for CLI/API access).

### `conf/rules.toml` — Alarm rules

```toml
[[rule]]
id = "cpu_high_single"
enabled = true
description = "CPU > 90% in a single measurement"
pluginid = "cpu"
metric = "percent"
condition = "gt"
threshold = 90.0
scope = "single"
severity = "warning"
notifications = ["email_admin"]
fire = "single"
executors = ["echo_alarm"]
```

### `conf/notifications.toml` — Email notification targets

```toml
[targets.email_admin]
type = "email"
to = "admin@example.com"
from = "monitor@example.com"
server = "mail.example.com"
port = 587
user = "admin"
# Password can be set via env var NOTIFY_EMAIL_PASSWORD
use_tls = true
```

**Security**: Set the password via the `NOTIFY_EMAIL_PASSWORD` environment variable instead of the config file.

### `conf/executors.toml` — Shell command executors

```toml
[executors.echo_alarm]
command = "echo 'Alarm {rule_id} on {agentid} metric {metric} value {value}' >> /tmp/alarms.log"
```

Template variables: `{rule_id}`, `{agentid}`, `{pluginid}`, `{metric}`, `{value}`, `{message}`, `{severity}`.

## API Endpoints

### Agents

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/agents/status` | agent | Update agent status (online/offline) |
| GET | `/agents` | agent | List all known agents |
| GET | `/groups` | agent | List groups with their assigned agents |
| GET | `/agents/<name>/plugins` | agent | List plugins assigned to an agent |
| GET | `/agents/<name>/plugins/<p>/metrics` | agent | List unique metric names |
| GET | `/agents/<name>/plugins/<p>/metrics/<m>` | agent | List metric data points (base64-encoded metric name) |

### Plugins

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/plugins` | agent | List plugins assigned to the authenticated agent |
| GET | `/plugins/<name>` | agent | Download plugin source code |
| GET | `/plugins/<name>/config` | agent | Get plugin configuration for the agent |

### Metrics

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/metrics` | agent | Ingest metrics from an agent |
| GET | `/metrics` | agent | Query all metrics (supports `?time-from=`, `?time-to=`, `?search=`) |
| GET | `/metrics/<agentid>` | agent | Query metrics for a specific agent |
| GET | `/metrics/<agentid>/<pluginid>` | agent | Query metrics for a specific agent+plugin |

### Alarms

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/alarms/<id>/ack` | agent | Acknowledge an alarm (only the owning agent) |

## Rule Engine

Rules define conditions on metrics. When a metric arrives, all matching rules are evaluated.

### Scopes

- **single**: Evaluate each individual measurement
- **moving_avg**: Evaluate the average of the last N measurements
- **count_ratio**: Evaluate how many of the last N measurements violate the threshold

### Fire Modes

- **single**: Only create one alarm per (agent, rule) until acknowledged
- **multi**: Create a new alarm for every violation
- **replace**: Acknowledge existing open alarms for (agent, rule, plugin, metric) and create a new alarm — always the latest

### Supported Conditions

`gt`, `ge`, `lt`, `le`, `eq`, `ne`

## Notifications

Email notifications are configured in `conf/notifications.toml`. The email password should be set via the `NOTIFY_EMAIL_PASSWORD` environment variable rather than the config file.

## Executors

Executors run shell commands when an alarm fires. Configured in `conf/executors.toml`. Template variables are replaced before execution.

## Plugin System

Plugins are Python classes that extend `PluginBase`. They are hosted on the server and downloaded by agents at runtime.

### Available Plugins

| Plugin | Description | Metrics |
|--------|-------------|---------|
| cpu | CPU usage | `percent` (float) |
| ram | RAM usage | `virtual_pct`, `swap_pct` |
| disk_usage | Disk usage per partition | `/<mountpoint>` (percent) |
| network | Network throughput | `*:bytes_sent`, `*:bytes_recv`, `*:tx_bytes_per_sec`, `*:rx_bytes_per_sec`, `tcp_open_connections` |
| ping | ICMP ping | `<host> success`, `<host> avg-time` |
| http | HTTP health check | `<name>:status_code`, `<name>:content_ok` |
| host | System information | `hostname`, `uptime`, `os`, `os_version`, `total_ram`, `cpu_count`, `cpu_physical_cores`, `cpu_model`, `swap_total`, `ip:*` |
| services | systemd service status | `<service>` (string: "running"/"stopped"/"failed") |
| temperature | Hardware sensors | `<sensor>:<label>` (celsius) |
| cert_valid | TLS certificate expiry | `<url>` (days remaining) |
| docker_host | Docker host stats | `containers_*`, `images_total`, `volumes_total`, `networks_total` |

## Database

SQLite file (`metrics.db`) created automatically. Tables:

- **metrics**: Stores all metric data points with agentid, pluginid, timestamp, metric name, and value
- **alarms**: Stores triggered alarms with severity, acknowledgment status, and link to the triggering metric

## Security

- All write endpoints require API key authentication via `agentid` + `X-API-Key` headers
- Read endpoints (GET /metrics) also require authentication
- Alarm acknowledgment is scoped to the owning agent
- Email password can be set via environment variable
- API keys are stored in `conf/apikeys.toml` (keep this file secure)