"""Crontab-style task scheduler for cron tasks assigned to agents.

The server evaluates the 5-field cron schedules on its local time and
marks tasks as due for the current minute. Agents poll GET /cron/due and
execute the referenced agent-side executor themselves.

No third-party cron library is used; schedules support the common
5-field syntax: minutes hours day-of-month month day-of-week with
'*', '*/n', 'a-b', 'a,c' and single values.
"""

import json
import os
import tempfile
import threading
import time
from datetime import datetime

from config import CONF_DIR, LOCAL_TZ
from core import logger

CRON_JSON = os.path.join(CONF_DIR, "cron_tasks.json")
TICK_INTERVAL_SECONDS = 15
FIVE_FIELDS_MAX = [
    (0, 59),    # minute
    (0, 23),    # hour
    (1, 31),    # day of month
    (1, 12),    # month
    (0, 7),     # day of week (0 and 7 = sunday, 1-6 = mon-sat, cron style)
]

_lock = threading.Lock()
# key (task_id, "YYYY-MM-DD HH:MM|agentid") -> monotonic timestamp of first serve
_fired: dict[tuple[str, str], float] = {}


# ── Cron expression parsing ──

def parse_field(field: str, lo: int, hi: int) -> set[int]:
    """Parse a single cron field like '*', '*/15', '1-5', '0,30' into a set."""
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            return set()
        if part == "*":
            values.update(range(lo, hi + 1))
            continue
        step = 1
        if "/" in part:
            base, _, step_str = part.partition("/")
            try:
                step = int(step_str)
            except ValueError:
                return set()
            if step <= 0:
                return set()
            part = base
            if part == "*":
                part = f"{lo}-{hi}"
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                a, b = int(a), int(b)
            except ValueError:
                return set()
            if a > b:
                return set()
            values.update(range(a, b + 1, step))
        else:
            try:
                value = int(part)
            except ValueError:
                return set()
            if value < lo or value > hi:
                return set()
            values.add(value)
    return values


def parse_schedule(schedule: str) -> list[set[int]] | None:
    """Parse a 5-field cron schedule; return None if invalid.

    The day-of-week field accepts the cron convention (0 and 7 = sunday,
    1-6 = monday-saturday) and is normalized to python's weekday() values.
    """
    fields = schedule.split()
    if len(fields) != 5:
        return None
    parsed = []
    for idx, (raw, (lo, hi)) in enumerate(zip(fields, FIVE_FIELDS_MAX)):
        values = parse_field(raw, lo, hi)
        if not values:
            return None
        if idx == 4:
            values = {(v % 7 + 6) % 7 for v in values}
        parsed.append(values)
    return parsed


def schedule_matches(parsed: list[set[int]], now: datetime) -> bool:
    """Check whether a parsed schedule matches the given local datetime."""
    if now.minute not in parsed[0]:
        return False
    if now.hour not in parsed[1]:
        return False
    if now.day not in parsed[2]:
        return False
    if now.month not in parsed[3]:
        return False
    return now.weekday() in parsed[4]


# ── Config I/O ──

def _load_tasks() -> dict:
    if os.path.exists(CRON_JSON):
        try:
            with open(CRON_JSON, encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Corrupt cron tasks config %s: %s", CRON_JSON, e)
            return {}
    return {}


def _save_tasks(data: dict) -> None:
    os.makedirs(CONF_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CONF_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CRON_JSON)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_executors() -> dict:
    fpath = os.path.join(CONF_DIR, "executors.json")
    try:
        with open(fpath, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _task_allowed_for_agent(task: dict, agentid: str) -> bool:
    """Agent restriction semantics identical to rules.py._rule_applies_to_agent."""
    agents = task.get("agents") or []
    if not agents:
        return True
    if task.get("agents_mode") == "include":
        return agentid in agents
    return agentid not in agents


def _resolve_executor(task: dict, executors: dict) -> dict | None:
    """Resolve the referenced executor, or None if unusable for agent-side runs."""
    executor = executors.get(task.get("executor_id"))
    if not executor:
        logger.warning("Cron task '%s': executor '%s' not found", task.get("id"), task.get("executor_id"))
        return None
    if not executor.get("enabled", True):
        logger.warning("Cron task '%s': executor '%s' is disabled", task.get("id"), task.get("executor_id"))
        return None
    if executor.get("execution_target", "server") != "agent":
        logger.warning("Cron task '%s': executor '%s' is not agent-side", task.get("id"), task.get("executor_id"))
        return None
    command = executor.get("command")
    if not command or not isinstance(command, str):
        logger.warning("Cron task '%s': executor '%s' has no command", task.get("id"), task.get("executor_id"))
        return None
    return executor


def due_tasks_for_agent(agentid: str) -> list[dict]:
    """Return due cron tasks for the given agent (enabled + allowed + not
    already fired this minute + usable executor). Marks them served.

    Schedules are interpreted in the configured LOCAL_TZ so a 04:00 cron
    entry always runs at 04:00 local time, regardless of the container's
    UTC bias.
    """
    now = datetime.now(LOCAL_TZ)
    minute_key = now.strftime("%Y-%m-%d %H:%M")

    with _lock:
        tasks = _load_tasks()
        executors = load_executors()
        result = []
        for task_id, task in tasks.items():
            if task.get("id") != task_id:
                task["id"] = task_id
            if not task.get("enabled", True):
                continue
            if not _task_allowed_for_agent(task, agentid):
                continue
            key = (task_id, f"{minute_key}|{agentid}")
            if key in _fired:
                continue
            parsed = parse_schedule(task.get("schedule", ""))
            if parsed is None or not schedule_matches(parsed, now):
                continue
            executor = _resolve_executor(task, executors)
            if executor is None:
                continue
            timeout = executor.get("timeout")
            result.append({
                "task_id": task_id,
                "title": task.get("title", task_id),
                "command": executor["command"],
                "timeout": int(timeout) if isinstance(timeout, (int, float)) and timeout > 0 else 60,
            })
            _fired[key] = now.timestamp()

        # Keep the marker map bounded.
        if len(_fired) > 5000:
            _fired.clear()

        if result:
            tasks_changed = False
            for item in result:
                task = tasks.get(item["task_id"])
                if task is not None and task.get("last_due_at") != now.isoformat():
                    task["last_due_at"] = now.isoformat()
                    tasks_changed = True
            if tasks_changed:
                _save_tasks(tasks)

    return result


# ── Scheduler thread (maintenance only — due matching is demand-driven) ──

def _loop() -> None:
    while True:
        try:
            # Forget fired markers older than two hours so a task that was
            # due before a restart fires again, but never twice per minute.
            cutoff = time.time() - 2 * 3600
            stale = [k for k, ts in _fired.items() if ts < cutoff]
            if stale:
                with _lock:
                    for k in stale:
                        _fired.pop(k, None)
        except Exception:
            logger.error("Cron scheduler maintenance failed", exc_info=True)
        threading.Event().wait(300)


def start_cron_scheduler() -> threading.Thread:
    thread = threading.Thread(
        target=_loop,
        name="pymon-cron-scheduler",
        daemon=True,
    )
    thread.start()
    return thread