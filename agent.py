"""pymon-agent -- Thin metric agent with subprocess plugin execution.

Each plugin is a standalone script invoked as a subprocess.
Contract:
  - Plugin reads config (JSON object) from stdin, one line
  - Plugin writes metrics (JSON object) to stdout, one line
  - Plugin exits 0 on success, non-zero on error (stderr for diagnostics)
  - Agent enforces a timeout (default 30s) per plugin run
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from hashlib import sha256
from typing import Optional

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

PLUGINS_DIR = "plugins"
POLL_INTERVAL = 5
PLUGIN_REFRESH_INTERVAL = 30
PLUGIN_TIMEOUT = 30
VERSION_CHECK_INTERVAL = 300
CONFIG_FILE = "agent.json"

SYSTEMD_SYSTEM_DIR = "/etc/systemd/system"


def _load_config() -> dict:
    try:
        with open(os.path.join(os.path.dirname(__file__), CONFIG_FILE)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _service_name(agentid: str) -> str:
    return f"pymon-agent-{agentid}.service"


def _install_service(agentid: str) -> None:
    """Install agent as a system-wide systemd service.
    Credentials are read from agent.json in WorkingDirectory at runtime.
    """
    python_exe   = sys.executable
    agent_script = os.path.abspath(__file__)
    agent_dir    = os.path.dirname(agent_script)
    svc_name     = _service_name(agentid)

    unit = (
        "[Unit]\n"
        f"Description=pymon agent -- {agentid}\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={agent_dir}\n"
        f"ExecStart={python_exe} {agent_script}\n"
        "Restart=always\n"
        "RestartSec=10\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )

    os.makedirs(SYSTEMD_SYSTEM_DIR, exist_ok=True)
    svc_path = os.path.join(SYSTEMD_SYSTEM_DIR, svc_name)
    with open(svc_path, "w") as f:
        f.write(unit)
    print(f"Written:  {svc_path}")

    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", svc_name], check=True)
    subprocess.run(["systemctl", "start",  svc_name], check=True)

    print(f"Service   {svc_name} installed and started.")
    print(f"Status:   systemctl status {svc_name}")
    print(f"Logs:     journalctl -u {svc_name} -f")


def _uninstall_service(agentid: str) -> None:
    """Stop, disable and remove the system-wide systemd service."""
    svc_name = _service_name(agentid)
    svc_path = os.path.join(SYSTEMD_SYSTEM_DIR, svc_name)

    subprocess.run(["systemctl", "stop",    svc_name], check=False)
    subprocess.run(["systemctl", "disable", svc_name], check=False)

    if os.path.exists(svc_path):
        os.remove(svc_path)
        print(f"Removed:  {svc_path}")
    else:
        print(f"Not found: {svc_path} (already removed?)")

    subprocess.run(["systemctl", "daemon-reload"], check=False)
    print(f"Service   {svc_name} uninstalled.")


cfg_file = _load_config()

parser = argparse.ArgumentParser(description="pymon Agent")
parser.add_argument("--server",    default=cfg_file.get("server"),  help="Server URL")
parser.add_argument("--agentid",   default=cfg_file.get("agentid"), help="Agent ID")
parser.add_argument("--api-key",   default=cfg_file.get("api_key"), type=str, help="API key for server auth")
parser.add_argument("--install",   action="store_true", help="Install as system-wide systemd service and exit")
parser.add_argument("--uninstall", action="store_true", help="Remove system-wide systemd service and exit")
args = parser.parse_args()

if args.install:
    if not args.agentid:
        print("ERROR: --agentid is required for --install")
        sys.exit(1)
    _install_service(args.agentid)
    sys.exit(0)

if args.uninstall:
    if not args.agentid:
        print("ERROR: --agentid is required for --uninstall")
        sys.exit(1)
    _uninstall_service(args.agentid)
    sys.exit(0)

if not args.server or not args.agentid or not args.api_key:
    print("ERROR: server, agentid, and api-key are required (pass as args or in agent.json)")
    sys.exit(1)


def _build_headers(extra: Optional[dict] = None) -> dict:
    base = {"agentid": args.agentid, "x-api-key": args.api_key}
    if extra:
        base.update(extra)
    return base


# ---------------------------------------------------------------------------
# Plugin file helpers
# ---------------------------------------------------------------------------

def plugin_path(name: str) -> str:
    return os.path.join(PLUGINS_DIR, f"{name}.py")


def plugin_hash(name: str) -> Optional[str]:
    try:
        with open(plugin_path(name), "rb") as f:
            return sha256(f.read()).hexdigest()
    except FileNotFoundError:
        return None


def fetch_plugin_list() -> Optional[list[str]]:
    try:
        resp = requests.get(f"{args.server}/plugins", headers=_build_headers())
        resp.raise_for_status()
        plugins = resp.json()
        if not isinstance(plugins, list):
            raise ValueError("plugin list response is not an array")
        return plugins
    except Exception as e:
        logging.error("Error fetching plugin list: %s", e)
        return None


def download_plugin(plugin: str) -> bool:
    try:
        resp = requests.get(f"{args.server}/plugins/{plugin}", headers=_build_headers())
        resp.raise_for_status()
        os.makedirs(PLUGINS_DIR, exist_ok=True)
        with open(plugin_path(plugin), "w", encoding="utf-8") as f:
            f.write(resp.text)
        logging.info("Plugin '%s' downloaded", plugin)
        return True
    except Exception as e:
        logging.error("Error downloading plugin '%s': %s", plugin, e)
        return False


def fetch_plugin_hash(plugin: str) -> Optional[str]:
    try:
        resp = requests.get(f"{args.server}/plugins/{plugin}/version", headers=_build_headers())
        resp.raise_for_status()
        plugin_hash_value = resp.json().get("hash")
        return plugin_hash_value if isinstance(plugin_hash_value, str) else None
    except Exception as e:
        logging.warning("Error fetching version for '%s': %s", plugin, e)
        return None


def fetch_plugin_config(plugin: str) -> Optional[dict]:
    try:
        resp = requests.get(f"{args.server}/plugins/{plugin}/config", headers=_build_headers())
        if resp.status_code == 403:
            logging.info("No active config for '%s'; skipping run", plugin)
            return None
        resp.raise_for_status()
        config = resp.json()
        if not isinstance(config, dict):
            raise ValueError("plugin config response is not an object")
        return config
    except Exception as e:
        logging.warning("Error fetching config for '%s': %s", plugin, e)
        return None


def sync_plugins(plugins: list[str]) -> bool:
    """Synchronize assigned plugins without stopping on an empty assignment."""
    fresh = fetch_plugin_list()
    if fresh is None:
        return False

    fresh = [plugin for plugin in fresh if plugin != "plugin_base"]
    active = []
    for plugin in fresh:
        remote_hash = fetch_plugin_hash(plugin)
        if remote_hash is not None:
            if plugin_hash(plugin) != remote_hash and not download_plugin(plugin):
                continue
        elif plugin_hash(plugin) is None and not download_plugin(plugin):
            continue
        active.append(plugin)

    previous = set(plugins)
    plugins[:] = active
    if previous != set(active):
        if active:
            logging.info("Assigned plugins: %s", active)
        else:
            logging.info("No plugins assigned; retrying in %ss", PLUGIN_REFRESH_INTERVAL)
    return True


# ---------------------------------------------------------------------------
# Plugin execution (subprocess model)
# ---------------------------------------------------------------------------

ERR_OK = 0
ERR_EXIT = 1
ERR_TIMEOUT = 2
ERR_NO_OUTPUT = 3
ERR_INVALID_JSON = 4
ERR_NOT_FOUND = 5
ERR_NO_INTERPRETER = 6


def run_plugin(plugin: str, config: dict) -> tuple[Optional[dict], float, int]:
    """Run a plugin and return (metrics, runtime_seconds, error_code)."""
    path = plugin_path(plugin)
    if not os.path.exists(path):
        logging.error("Plugin file not found: %s", path)
        return None, 0.0, ERR_NOT_FOUND

    start = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, path],
            input=json.dumps(config or {}),
            capture_output=True, text=True,
            timeout=PLUGIN_TIMEOUT,
        )
    except FileNotFoundError:
        logging.error("Python interpreter not found for plugin '%s'", plugin)
        return None, time.time() - start, ERR_NO_INTERPRETER
    except subprocess.TimeoutExpired:
        logging.warning("Plugin '%s' timed out after %ss; continuing", plugin, PLUGIN_TIMEOUT)
        return None, time.time() - start, ERR_TIMEOUT

    runtime = time.time() - start

    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        if stderr:
            logging.warning("Plugin '%s' stderr: %s", plugin, stderr)
        return None, runtime, ERR_EXIT

    output = proc.stdout.strip()
    if not output:
        return None, runtime, ERR_NO_OUTPUT

    try:
        return json.loads(output), runtime, ERR_OK
    except json.JSONDecodeError as e:
        logging.error("Plugin '%s' returned invalid JSON: %s", plugin, e)
        return None, runtime, ERR_INVALID_JSON


# ---------------------------------------------------------------------------
# Agent lifecycle
# ---------------------------------------------------------------------------

def send_status(status: str):
    try:
        requests.post(
            f"{args.server}/agents/status",
            json={"status": status},
            headers=_build_headers(),
            timeout=10,
        )
    except Exception as e:
        logging.error("Error sending status '%s': %s", status, e)


def _run_agent_executors(response_data: object) -> None:
    if not isinstance(response_data, dict):
        logging.warning("POST /metrics returned an unexpected response shape")
        return

    executors = response_data.get("executors", [])
    if not isinstance(executors, list):
        logging.warning("POST /metrics returned an invalid executors field")
        return

    for executor in executors:
        if not isinstance(executor, dict):
            logging.warning("Ignoring malformed agent-side executor: %r", executor)
            continue

        command = executor.get("command")
        executor_id = executor.get("id", "")
        if not isinstance(command, str) or not command:
            logging.warning("Skipping agent-side executor '%s': missing command", executor_id)
            continue

        logging.info("Running agent-side executor '%s': %s", executor_id, command)
        try:
            subprocess.run(command, shell=True, check=False, timeout=30)
        except subprocess.TimeoutExpired:
            logging.error("Executor '%s' timed out", executor_id)
        except (OSError, subprocess.SubprocessError) as e:
            logging.error("Executor '%s' failed: %s", executor_id, e)
        else:
            logging.info("Executor '%s' finished", executor_id)


def post_metrics(plugin: str, metrics_list: list[dict], timestamp: str):
    payload = {
        "pluginid": plugin,
        "agentid": args.agentid,
        "metrics": metrics_list,
        "timestamp": timestamp,
    }
    try:
        resp = requests.post(
            f"{args.server}/metrics",
            json=payload,
            headers=_build_headers(),
            timeout=15,
        )
        if not resp.ok:
            logging.warning("POST /metrics returned %s for '%s'", resp.status_code, plugin)
        else:
            try:
                response_data = resp.json()
            except ValueError as e:
                logging.warning("POST /metrics returned invalid JSON for '%s': %s", plugin, e)
                return
            _run_agent_executors(response_data)
    except Exception as e:
        logging.error("Error posting metrics for '%s': %s", plugin, e)


def signal_handler(sig, frame):
    logging.info("Shutting down... sending offline status")
    send_status("offline")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Self-update
# ---------------------------------------------------------------------------

def self_update():
    """Check for a newer agent version on the server and replace ourselves."""
    try:
        resp = requests.get(f"{args.server}/agent/version", headers=_build_headers(), timeout=10)
        resp.raise_for_status()
        remote_hash = resp.json().get("hash")
        if not remote_hash:
            return
    except Exception:
        return

    with open(__file__, "rb") as f:
        local_hash = sha256(f.read()).hexdigest()

    if local_hash == remote_hash:
        return

    logging.info("New agent version detected, downloading...")
    try:
        resp = requests.get(
            f"{args.server}/agent/download",
            headers=_build_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        new_code = resp.text
    except Exception as e:
        logging.error("Failed to download new agent version: %s", e)
        return

    backup = __file__ + ".bak"
    try:
        with open(backup, "w", encoding="utf-8") as f:
            f.write(new_code)
        os.replace(backup, __file__)
        logging.info("Agent updated. Restarting...")
        os.execv(sys.executable, [sys.executable, __file__] + sys.argv[1:])
    except Exception as e:
        logging.error("Failed to apply agent update: %s", e)
        if os.path.exists(backup):
            os.remove(backup)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

if __name__ == "__main__":
    os.makedirs(PLUGINS_DIR, exist_ok=True)

    plugins: list[str] = []
    sync_plugins(plugins)
    if not plugins:
        logging.info("No plugins assigned; waiting for server configuration.")

    send_status("online")
    logging.info("Agent online.")

    last_plugin_refresh = time.time()
    last_version_check = 0.0
    last_run: dict[str, float] = {}
    plugin_sleeps: dict[str, int] = {}

    while True:
        now = time.time()

        # Periodic plugin list refresh
        if now - last_plugin_refresh >= PLUGIN_REFRESH_INTERVAL:
            if sync_plugins(plugins):
                for stale in (set(last_run) | set(plugin_sleeps)) - set(plugins):
                    last_run.pop(stale, None)
                    plugin_sleeps.pop(stale, None)
            last_plugin_refresh = now

        # Periodic self-update check -- disabled in dev
        # self_update()
        # last_version_check = now

        # Run plugins respecting per-plugin sleep interval
        ts = datetime.now(timezone.utc).isoformat()
        for plugin in plugins:
            if plugin_hash(plugin) is None:
                continue

            last_ts = last_run.get(plugin, 0.0)
            plugin_sleep = plugin_sleeps.get(plugin, 0)
            if plugin_sleep and now - last_ts < plugin_sleep:
                continue

            # Fetch the current config immediately before a due run.
            cfg = fetch_plugin_config(plugin)
            if cfg is None:
                continue
            try:
                plugin_sleep = max(1, int(cfg.get("sleep", 60)))
            except (TypeError, ValueError):
                plugin_sleep = 60
            plugin_sleeps[plugin] = plugin_sleep
            if last_ts and now - last_ts < plugin_sleep:
                continue

            metrics, runtime, err_code = run_plugin(plugin, cfg)

            # Normalize: plugin returns dict -> list of {key: value}
            if isinstance(metrics, dict):
                metrics_list = [{k: v} for k, v in metrics.items()]
            elif isinstance(metrics, list):
                metrics_list = metrics  # already a list
            else:
                metrics_list = []

            metrics_list.append({"agent:runtime": round(runtime, 3)})
            metrics_list.append({"agent:error": err_code})

            post_metrics(plugin, metrics_list, ts)
            last_run[plugin] = now

        time.sleep(POLL_INTERVAL)
