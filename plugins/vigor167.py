#!/usr/bin/env python3
"""DrayTek Vigor 167 DSL monitoring in bridge mode.

The collector logs in through the modem's read-only management CLI over SSH.
It reads the ``exec dslinfo`` summary and the DrayOS 5 monitoring page
(``config Monitoring DSL_Status Monitoring_DSL_General`` + ``show all``) for
the stream and error tables; classic ``vdsl status`` commands are a fallback
for older firmware.  It never sends a ``set``/``edit``/``save``/``reboot``
command and never emits credentials, command output or session data as metrics.

The SSH layer uses paramiko.  It is imported lazily so the parser can be tested
without the optional dependency; the agent image installs it via
``pymon-agent/requirements.txt``.
"""

import json
import os
import re
import socket
import sys
import time


__schema__ = {
    "label": "DrayTek Vigor 167 (DSL)",
    "description": "DrayTek Vigor 167 DSL modem monitoring via SSH CLI (bridge mode)",
    "fields": [
        {
            "key": "sleep",
            "label": "Interval (s)",
            "type": "number",
            "default": 30,
            "min": 30,
        },
        {
            "key": "host",
            "label": "Vigor hostname or IP",
            "type": "string",
            "default": "192.168.184.2",
        },
        {
            "key": "ssh_port",
            "label": "SSH port",
            "type": "number",
            "default": 22,
            "min": 1,
            "max": 65535,
        },
        {
            "key": "username",
            "label": "Vigor username",
            "type": "string",
            "default": "admin",
        },
        {"key": "password", "label": "Vigor password", "type": "password"},
        {
            "key": "timeout",
            "label": "SSH timeout (s)",
            "type": "number",
            "default": 10,
            "min": 3,
            "max": 30,
        },
        {
            "key": "verify_host_key",
            "label": "Verify SSH host key",
            "type": "boolean",
            "default": False,
            "optional": True,
        },
        {
            "key": "known_hosts",
            "label": "known_hosts file (optional)",
            "type": "string",
            "default": "",
            "optional": True,
        },
        {
            "key": "dsl_command",
            "label": "DSL CLI command",
            "type": "string",
            "default": "exec dslinfo",
            "optional": True,
        },
        {
            "key": "state_file",
            "label": "State file (optional)",
            "type": "string",
            "default": "",
            "optional": True,
        },
    ],
}

DEFAULT_DSL_COMMAND = "exec dslinfo"
DEFAULT_SSH_PORT = 22
MONITORING_PAGE_COMMAND = "config Monitoring DSL_Status Monitoring_DSL_General"
MONITORING_SHOW_COMMAND = "show all"

PLACEHOLDERS = {"", "---", "[---]", "(nil)", "-", "n/a", "none", "--"}

# The error table is explicitly Near End / Far End on the Vigor 167.  Keep the
# old aliases as a transition path for dashboards/rules created for vigor130.
COUNTER_DIRECTIONS = ("_near_end", "_far_end")
LEGACY_COUNTER_DIRECTIONS = {"_near_end": "_downstream", "_far_end": "_upstream"}
COUNTER_BASE = {
    "crc": "crc",
    "fecs": "fecs",
    "es": "es_seconds",
    "ses": "ses_seconds",
    "loss": "loss_seconds",
    "uas": "uas_seconds",
    "hec_errors": "hec_errors",
    "rs_corrections": "rs_corrections",
    "los_failure": "los_failures",
    "lof_failure": "lof_failures",
    "lpr_failure": "lpr_failures",
    "ncd_failure": "ncd_failures",
    "lcd_failure": "lcd_failures",
    "nfec": "nfec",
    "rfec": "rfec",
    "lysmb": "lysmb",
}
COUNTER_ALIASES = {
    "crc": "crc",
    "crc count": "crc",
    "crc errors": "crc",
    "fecs": "fecs",
    "fec": "fecs",
    "fec count": "fecs",
    "es": "es",
    "es count": "es",
    "errored seconds": "es",
    "ses": "ses",
    "ses count": "ses",
    "severely errored seconds": "ses",
    "loss": "loss",
    "loss count": "loss",
    "loss of signal seconds": "loss",
    "uas": "uas",
    "uas count": "uas",
    "unavailable seconds": "uas",
    "hec": "hec_errors",
    "hec errors": "hec_errors",
    "hec error": "hec_errors",
    "rs corrections": "rs_corrections",
    "rs correction": "rs_corrections",
    "los failure": "los_failure",
    "lof failure": "lof_failure",
    "lpr failure": "lpr_failure",
    "ncd failure": "ncd_failure",
    "lcd failure": "lcd_failure",
    "los": "los_failure",
    "lof": "lof_failure",
    "lpr": "lpr_failure",
    "ncd": "ncd_failure",
    "lcd": "lcd_failure",
    "nfec": "nfec",
    "rfec": "rfec",
    "lysmb": "lysmb",
}
PAIR_ALIASES = {
    "actual rate": ("actual_rate", None),
    "actual line rate": ("actual_rate", None),
    "line rate": ("actual_rate", None),
    "sync rate": ("actual_rate", None),
    "attainable rate": ("attainable_rate", None),
    "attainable line rate": ("attainable_rate", None),
    "snr margin": ("snr_margin", None),
    "snr": ("snr_margin", None),
    "cur snr margin": ("snr_margin", None),
    "current snr margin": ("snr_margin", None),
    "attenuation": ("attenuation", None),
    "line attenuation": ("attenuation", None),
    "current attenuation": ("attenuation", None),
    "cur attenuation": ("attenuation", None),
    "path mode": ("path_mode", None),
    "interleave depth": ("interleave_depth", None),
}
SCALAR_ALIASES = {
    "status": "line_state",
    "state": "line_state",
    "line state": "line_state",
    "line status": "line_state",
    "dsl status": "line_state",
    "dsl state": "line_state",
    "running mode": "mode",
    "mode": "mode",
    "dsl mode": "mode",
    "xdsl mode": "mode",
    "profile": "profile",
    "vdsl profile": "profile",
    "dsl profile": "profile",
    "annex": "annex",
    "annex type": "annex",
    "dsl version": "dsl_version",
    "dsl firmware version": "dsl_version",
    "firmware version": "dsl_version",
    "modem code": "dsl_version",
    "line uptime": "line_uptime_seconds",
    "line up time": "line_uptime_seconds",
    "dsl uptime": "line_uptime_seconds",
    "link uptime": "line_uptime_seconds",
    "link up time": "line_uptime_seconds",
    "system uptime": "system_uptime_seconds",
    "system up time": "system_uptime_seconds",
}
SYNC_METRICS = {
    "actual_rate": "sync_rate_kbps",
    "attainable_rate": "attainable_rate_kbps",
    "snr_margin": "snr_db",
    "attenuation": "attenuation_db",
    "interleave_depth": "interleave_depth",
}
STATE_VERSION = 1

# Build the label lookup table once.  Directional aliases let both the classic
# ``NE CRC Count / FE CRC Count`` layout and DrayOS 5's
# ``Downstream_Line_Rate / Upstream_Line_Rate`` fields resolve to one pair.
FIELD_ALIASES = {}
for _alias, _key in SCALAR_ALIASES.items():
    FIELD_ALIASES[_alias] = ("scalar", _key, None)
for _alias, (_key, _direction) in PAIR_ALIASES.items():
    FIELD_ALIASES[_alias] = ("pair", _key, _direction)
for _alias, _key in COUNTER_ALIASES.items():
    FIELD_ALIASES[_alias] = ("counter", _key, None)

_DIRECTION_PREFIXES = (
    ("near end", 0),
    ("far end", 1),
    ("downstream", 0),
    ("upstream", 1),
    ("near", 0),
    ("far", 1),
    ("ne", 0),
    ("fe", 1),
    ("ds", 0),
    ("us", 1),
)
for _prefix, _direction in _DIRECTION_PREFIXES:
    for _alias, (_key, _alias_direction) in PAIR_ALIASES.items():
        if _alias_direction is None:
            FIELD_ALIASES[f"{_prefix} {_alias}"] = ("pair", _key, _direction)
    for _alias, _key in COUNTER_ALIASES.items():
        FIELD_ALIASES[f"{_prefix} {_alias}"] = ("counter", _key, _direction)

# A label starts at the beginning of a line or after at least two spaces.  This
# keeps the regex from treating the "Mode" inside "Power Management Mode" as a
# separate field.
_LABEL_RE = re.compile(r"(?:^|\s{2,})([A-Za-z][A-Za-z0-9 ./()_\-]{0,40}?)\s*[:=]\s*")
_ROW_RE = re.compile(r"^([A-Za-z][A-Za-z0-9 ./()_\-]{0,40}?)\s{2,}(.+)$")
_NUMBER_RE = re.compile(
    r"([-+]?\d+(?:[.,]\d+)?)\s*(kbps|mbps|gbps|bps|db|dB|s|sec|secs|seconds)?",
    re.IGNORECASE,
)
_PAGER_RE = re.compile(r"(?i)(?:--\s*more\s*--|<more>|press\s+(?:any\s+)?key[^\n]*)")
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")
_PROMPT_RE = re.compile(r"(?m)^[^\s]{1,64}[>#]\s*$")


def _label_norm(value):
    text = str(value or "").strip(" .:\t\r\n")
    text = text.replace("_", " ")
    return re.sub(r"\s+", " ", text).lower()


def _san(value):
    value = str(value or "").strip()
    value = re.sub(r"\s+", "_", value)
    return re.sub(r"[^A-Za-z0-9_.\-/]", "", value)


def _to_duration(value):
    """Parse the Vigor's d/h/m/s or clock-style uptime into seconds."""
    if value is None:
        return None
    # DrayOS 5 reports plain seconds as a number (System_Uptime: 2681).
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower()
    if text in PLACEHOLDERS:
        return None
    if re.fullmatch(r"\d+(?:[.,]\d+)?", text):
        return int(float(text.replace(",", ".")))

    clock = re.fullmatch(r"(\d+):(\d+):(\d+)(?::(\d+))?", text)
    if clock:
        parts = [int(p) for p in clock.groups() if p is not None]
        if len(parts) == 4:  # days:hours:minutes:seconds
            return parts[0] * 86400 + parts[1] * 3600 + parts[2] * 60 + parts[3]
        return parts[0] * 3600 + parts[1] * 60 + parts[2]

    total = 0.0
    found = False
    for pattern, multiplier in (
        (r"(\d+(?:[.,]\d+)?)\s*(?:days?|d)\b", 86400),
        (r"(\d+(?:[.,]\d+)?)\s*(?:hours?|h)\b", 3600),
        (r"(\d+(?:[.,]\d+)?)\s*(?:minutes?|mins?|m)\b", 60),
        (r"(\d+(?:[.,]\d+)?)\s*(?:seconds?|secs?|s)\b", 1),
    ):
        match = re.search(pattern, text)
        if match:
            total += float(match.group(1).replace(",", ".")) * multiplier
            found = True
    return int(total) if found else None


def _canonical_state(value):
    text = re.sub(r"[^A-Za-z0-9]+", "", str(value or "")).lower()
    if text.endswith("showtime") or text == "showtime":
        return "SHOWTIME"
    if text.endswith("training") or text == "training":
        return "TRAINING"
    return _san(value).upper()


def _lookup_field(label):
    """Resolve a CLI label to ``(kind, key, direction)`` or ``None``."""
    normal = _label_norm(label)
    if normal in FIELD_ALIASES:
        return FIELD_ALIASES[normal]

    # Handle variants such as "SNR_Downstream", "CRC (Near End)" or
    # "Downstream Line Attenuation" without enumerating every combination.
    cleaned = normal.replace("(", " ").replace(")", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    direction = None
    for token, index in _DIRECTION_PREFIXES:
        if re.search(rf"\b{re.escape(token)}\b", cleaned):
            direction = index
            cleaned = re.sub(rf"\b{re.escape(token)}\b", " ", cleaned)
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            break
    if cleaned in FIELD_ALIASES:
        kind, key, alias_direction = FIELD_ALIASES[cleaned]
        return kind, key, direction if direction is not None else alias_direction
    return None


def _strip_ansi(text):
    return _ANSI_RE.sub("", text or "")


def _process_backspaces(text):
    result = []
    for char in text:
        if char == "\b":
            if result:
                result.pop()
        else:
            result.append(char)
    return "".join(result)


def _clean_lines(text):
    """Return non-empty, de-ANSI'd output lines without pager artefacts."""
    text = _strip_ansi(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _process_backspaces(text)
    lines = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if _PAGER_RE.fullmatch(line):
            continue
        if re.fullmatch(r"[-=_]{3,}", line):
            continue
        lines.append(line)
    return lines


def _clean_cli_output(text, command=None, prompt=None):
    """Remove the echoed command, terminal prompt and pager noise."""
    text = _strip_ansi(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _process_backspaces(text)
    lines = text.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    if command and lines:
        first = lines[0].strip()
        if first == command.strip() or first.endswith(command.strip()):
            lines.pop(0)
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if prompt and stripped == prompt:
            continue
        if re.fullmatch(r"[^\s]{0,64}[>#]", stripped):
            continue
        cleaned.append(line.rstrip())
    if prompt and cleaned:
        last = cleaned[-1].rstrip()
        if last.endswith(prompt):
            cleaned[-1] = last[: -len(prompt)].rstrip()
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    return "\n".join(cleaned)


def _extract_numbers(text):
    """Return numeric values, normalising rate units to kbit/s."""
    # Some firmware prints "7. 3 dB" with a space after the decimal point.
    text = re.sub(r"(\d)\s*\.\s*(\d)", r"\1.\2", str(text or ""))
    values = []
    for match in _NUMBER_RE.finditer(text):
        try:
            number = float(match.group(1).replace(",", "."))
        except ValueError:
            continue
        unit = (match.group(2) or "").lower()
        if unit == "mbps":
            number *= 1000
        elif unit == "gbps":
            number *= 1_000_000
        elif unit == "bps":
            number /= 1000
        values.append(int(number) if number.is_integer() else number)
    return values


def _parse_numeric_pair(value):
    parts = [part for part in re.split(r"\s*/\s*", str(value or "")) if part.strip()]
    if len(parts) >= 2:
        first = _extract_numbers(parts[0])
        second = _extract_numbers(parts[1])
        pair = [first[0] if first else None, second[0] if second else None]
        return pair if any(item is not None for item in pair) else None
    numbers = _extract_numbers(value)
    if not numbers:
        return None
    return numbers[:2]


def _parse_text_pair(value):
    parts = [part.strip() for part in re.split(r"\s*/\s*", str(value or ""))]
    if len(parts) >= 2:
        return [part for part in parts[:2] if part]
    parts = [part for part in re.split(r"\s{2,}", str(value or "").strip()) if part]
    if len(parts) >= 2:
        return parts[:2]
    text = str(value or "").strip()
    return [text] if text else None


def _direction_kind(label):
    """Map a directional CLI label to the counter-column convention."""
    normal = _label_norm(label)
    if re.search(r"\bnear end\b|\bne\b|\bfar end\b|\bfe\b", normal):
        return "near_end_far_end"
    if re.search(r"\bdownstream\b|\bds\b|\bupstream\b|\bus\b", normal):
        return "downstream_upstream"
    return None


def _apply_field(parsed, label, value, table_direction=None):
    resolved = _lookup_field(label)
    if not resolved:
        return
    kind, key, direction = resolved
    value = str(value or "").strip()
    if not value or value.lower() in PLACEHOLDERS:
        return

    if kind == "scalar":
        if key == "line_state":
            parsed[key] = _canonical_state(value)
        elif key in ("line_uptime_seconds", "system_uptime_seconds"):
            duration = _to_duration(value)
            if duration is not None:
                parsed[key] = duration
        else:
            if key == "dsl_version":
                value = re.sub(r"\[[^\]]*\]", "", value).strip()
            if value:
                parsed[key] = value
        return

    if kind == "counter":
        pair = _parse_numeric_pair(value)
        if pair:
            if direction is None:
                parsed[key] = pair
            else:
                existing = parsed.get(key)
                if not isinstance(existing, list):
                    existing = []
                if len(existing) == 0:
                    existing = [None, None]
                elif len(existing) == 1:
                    existing = [existing[0], None]
                existing[direction] = pair[0] if len(pair) == 1 else pair[direction]
                parsed[key] = existing
            counter_kind = table_direction or _direction_kind(label)
            if counter_kind:
                parsed.setdefault("_counter_directions", set()).add(counter_kind)
        return

    # kind == "pair"
    pair = _parse_text_pair(value) if key == "path_mode" else _parse_numeric_pair(value)
    if not pair:
        return
    if direction is None:
        parsed[key] = pair
        return
    existing = parsed.get(key)
    if not isinstance(existing, list):
        existing = []
    if len(existing) == 0:
        existing = [None, None]
    elif len(existing) == 1:
        existing = [existing[0], None]
    existing[direction] = pair[0] if len(pair) == 1 else pair[direction]
    parsed[key] = existing


def parse_cli_output(text):
    """Convert Vigor CLI text (DrayOS 5 or classic vdsl status) to the model."""
    parsed = {}
    table_direction = None
    for line in _clean_lines(text):
        low = line.lower()
        if "near end" in low and "far end" in low and ":" not in line:
            table_direction = "near_end_far_end"
        elif "downstream" in low and "upstream" in low and ":" not in line:
            table_direction = "downstream_upstream"
        if not parsed.get("annex") and "hw:" in low and "annex" in low:
            annex = re.search(r"\bannex\s+([A-Za-z])\b", line, re.IGNORECASE)
            if annex:
                parsed["annex"] = f"ANNEX {annex.group(1).upper()}"

        matches = list(_LABEL_RE.finditer(line))
        if matches:
            for index, match in enumerate(matches):
                end = (
                    matches[index + 1].start()
                    if index + 1 < len(matches)
                    else len(line)
                )
                _apply_field(
                    parsed, match.group(1), line[match.end() : end], table_direction
                )
            continue

        row = _ROW_RE.match(line)
        if row:
            _apply_field(parsed, row.group(1), row.group(2), table_direction)

    # Classic ``vdsl status`` reports the VDSL profile (17A, 35B, ...) as
    # "Running Mode".  DrayOS 5's dslinfo reports Mode and Profile separately;
    # only infer the pair when the firmware did not provide a profile.
    mode = parsed.get("mode")
    if (
        mode
        and not parsed.get("profile")
        and re.fullmatch(r"(?:8|12|17|30|35)[a-zA-Z]", str(mode).strip())
    ):
        parsed["profile"] = mode
        parsed["mode"] = "VDSL2"

    directions = parsed.pop("_counter_directions", set())
    if directions:
        if "near_end_far_end" in directions:
            parsed["counter_direction"] = "near_end_far_end"
        elif "downstream_upstream" in directions:
            parsed["counter_direction"] = "downstream_upstream"
    return {key: value for key, value in parsed.items() if value not in (None, "")}


def _walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _first_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return item
    return None


def _json_payload(text):
    text = _strip_ansi(text or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except ValueError:
        return None


def _pair_from_values(first, second, key):
    if key == "path_mode":
        values = []
        for value in (first, second):
            text = str(value or "").strip()
            values.append(None if text.lower() in PLACEHOLDERS else text)
        return values
    pair = []
    for value in (first, second):
        numbers = _extract_numbers(value)
        pair.append(numbers[0] if numbers else None)
    return pair


def parse_monitoring_json(text):
    """Parse the JSON emitted by ``show all`` on the DSL monitoring page.

    DrayOS 5 returns the same structure as the web API's
    ``0MONITORING_DSL_GENERAL`` PID, so the stream and error tables can be
    decoded without screen scraping.
    """
    payload = _json_payload(text)
    if not payload:
        return {}
    block = None
    for item in _walk_dicts(payload):
        if "0MONITORING_DSL_GENERAL" in item:
            block = _first_dict(item["0MONITORING_DSL_GENERAL"])
            break
    if not block:
        return {}

    parsed = {}
    for source, key in (
        ("Status", "line_state"),
        ("Mode", "mode"),
        ("Profile", "profile"),
        ("Annex", "annex"),
        ("DSL_Version", "dsl_version"),
    ):
        value = str(block.get(source) or "").strip()
        if not value:
            continue
        parsed[key] = _canonical_state(value) if key == "line_state" else value
    if block.get("Line_Uptime"):
        duration = _to_duration(block["Line_Uptime"])
        if duration is not None:
            parsed["line_uptime_seconds"] = duration

    for row in _walk_dicts(block.get("Stream_Table", [])):
        if not isinstance(row, dict) or not row.get("Name"):
            continue
        resolved = _lookup_field(row["Name"])
        if not resolved:
            continue
        kind, key, _direction = resolved
        if kind not in ("pair", "counter"):
            continue
        pair = _pair_from_values(row.get("Downstream"), row.get("Upstream"), key)
        if any(value is not None for value in pair):
            parsed[key] = pair

    end_rows = False
    for row in _walk_dicts(block.get("End_Table", [])):
        if not isinstance(row, dict) or not row.get("Name"):
            continue
        resolved = _lookup_field(row["Name"])
        if not resolved:
            continue
        kind, key, _direction = resolved
        if kind not in ("pair", "counter"):
            continue
        end_rows = True
        pair = _pair_from_values(row.get("Near_End"), row.get("Far_End"), key)
        if any(value is not None for value in pair):
            parsed[key] = pair

    if end_rows:
        parsed["counter_direction"] = "near_end_far_end"
    return parsed


def _fresh_state():
    return {
        "_version": STATE_VERSION,
        "baseline": False,
        "last_poll_at": None,
        "last_line_state": None,
        "last_line_uptime": None,
        "last_system_uptime": None,
        "counters": {},
        "last_counter_ts": None,
        "last_showtime_loss_at": None,
        "last_outage_seconds": None,
        "resync_count": 0,
        "reboot_count": 0,
        "scrape_fail_count": 0,
    }


def _load_state(path):
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
        if not isinstance(state, dict):
            return _fresh_state()
    except (OSError, ValueError):
        return _fresh_state()
    for key, default in _fresh_state().items():
        state.setdefault(key, default)
    state["_version"] = STATE_VERSION
    return state


def _save_state(path, state):
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        temporary = f"{path}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
        os.replace(temporary, path)
    except OSError:
        pass


def _pair_metrics(prefix, pair, output, suffixes=("_downstream", "_upstream")):
    if not isinstance(pair, (list, tuple)):
        return
    for value, suffix in zip(pair, suffixes):
        if value is not None:
            output[f"vigor_dsl_{prefix}{suffix}"] = value


def compute_poll(state, parsed, now=None):
    """Update persistent line/counter state and return this poll's metrics."""
    now = time.time() if now is None else now
    metrics = {}
    line_state = parsed.get("line_state")
    if not line_state:
        metrics.update(
            {"vigor_dsl_scrape_success": 0, "vigor_dsl_error": "parse_failed"}
        )
        state["scrape_fail_count"] = state.get("scrape_fail_count", 0) + 1
        return metrics

    metrics["vigor_dsl_scrape_success"] = 1
    metrics["vigor_dsl_error"] = ""
    state["scrape_fail_count"] = 0
    state["last_poll_at"] = now
    current_state = _canonical_state(line_state)
    metrics["vigor_dsl_line_state"] = _san(current_state)
    line_up = 1 if current_state == "SHOWTIME" else 0

    for source, metric in (
        ("mode", "mode"),
        ("profile", "profile"),
        ("annex", "annex"),
        ("dsl_version", "version"),
    ):
        if parsed.get(source):
            value = _san(parsed[source])
            metrics[f"vigor_dsl_{metric}"] = value
            if source == "mode":
                # Vigor 167 exposes Mode where the legacy plugin exposed both
                # Type and Running Mode.  Emit both compatibility names.
                metrics["vigor_dsl_type"] = value
                metrics["vigor_dsl_running_mode"] = value
    _pair_metrics("path_mode", parsed.get("path_mode"), metrics)
    if parsed.get("line_uptime_seconds") is not None:
        metrics["vigor_dsl_line_uptime_seconds"] = int(parsed["line_uptime_seconds"])
    if parsed.get("system_uptime_seconds") is not None:
        metrics["vigor_dsl_system_uptime_seconds"] = int(
            parsed["system_uptime_seconds"]
        )

    for key, prefix in SYNC_METRICS.items():
        _pair_metrics(prefix, parsed.get(key), metrics)

    was_baseline = bool(state.get("baseline"))
    previous_state = state.get("last_line_state")
    loss_at = state.get("last_showtime_loss_at")
    if not was_baseline:
        state["baseline"] = True
    else:
        if previous_state == "SHOWTIME" and current_state != "SHOWTIME" and not loss_at:
            state["last_showtime_loss_at"] = now
        elif previous_state != "SHOWTIME" and current_state == "SHOWTIME" and loss_at:
            duration = max(0.0, now - loss_at)
            state["last_outage_seconds"] = duration
            metrics["vigor_dsl_last_outage_seconds"] = round(duration, 1)
            state["last_showtime_loss_at"] = None
        metrics["vigor_dsl_line_up"] = line_up
    state["last_line_state"] = current_state

    previous_counter_ts = state.get("last_counter_ts")
    elapsed = (
        now - previous_counter_ts
        if previous_counter_ts and now >= previous_counter_ts
        else None
    )
    any_counter_reset = False
    counters = state.setdefault("counters", {})
    for key, base in COUNTER_BASE.items():
        pair = parsed.get(key)
        if not isinstance(pair, (list, tuple)):
            continue
        for value, direction in zip(pair, COUNTER_DIRECTIONS):
            if value is None:
                continue
            counter_key = f"{base}{direction}"
            previous = counters.get(counter_key)
            for suffix in (direction, LEGACY_COUNTER_DIRECTIONS[direction]):
                metrics[f"vigor_dsl_{base}_total{suffix}"] = value
            counters[counter_key] = value
            if previous is None:
                continue
            delta = value - previous
            if delta < 0:
                any_counter_reset = True
                continue
            if elapsed and elapsed > 0:
                for suffix in (direction, LEGACY_COUNTER_DIRECTIONS[direction]):
                    metrics[f"vigor_dsl_{base}_delta{suffix}"] = round(delta, 3)
                    if delta > 0:
                        metrics[f"vigor_dsl_{base}_rate_perhour{suffix}"] = round(
                            delta / elapsed * 3600, 3
                        )

    previous_line_uptime = state.get("last_line_uptime")
    current_line_uptime = parsed.get("line_uptime_seconds")
    previous_system_uptime = state.get("last_system_uptime")
    current_system_uptime = parsed.get("system_uptime_seconds")
    line_reset = (
        current_line_uptime is not None
        and previous_line_uptime is not None
        and previous_line_uptime > 60
        and current_line_uptime < previous_line_uptime - 60
    )
    system_reset = (
        current_system_uptime is not None
        and previous_system_uptime is not None
        and previous_system_uptime > 60
        and current_system_uptime < previous_system_uptime - 60
    )
    if was_baseline:
        if system_reset:
            state["reboot_count"] = state.get("reboot_count", 0) + 1
        elif any_counter_reset or line_reset:
            state["resync_count"] = state.get("resync_count", 0) + 1

    state["last_counter_ts"] = now
    state["last_line_uptime"] = current_line_uptime
    state["last_system_uptime"] = current_system_uptime
    metrics["vigor_dsl_resync_total"] = state.get("resync_count", 0)
    metrics["vigor_dsl_reboot_total"] = state.get("reboot_count", 0)
    return metrics


# ---------------------------------------------------------------------------
# SSH transport
# ---------------------------------------------------------------------------


class Vigor167SSHClient:
    """Read-only interactive SSH client for the DrayOS management CLI.

    DrayTek's SSH server accepts the transport-level ``none`` authentication
    and then runs ``draysh``, which asks for ``Username:`` / ``Password:`` in
    the channel.  Some firmware revisions accept normal SSH password auth
    instead, so both paths are supported.
    """

    def __init__(
        self,
        host,
        port=DEFAULT_SSH_PORT,
        username="admin",
        password="",
        timeout=8,
        verify_host_key=False,
        known_hosts="",
    ):
        self.host = host
        self.port = int(port)
        self.username = username or "admin"
        self.password = password or ""
        self.timeout = int(timeout)
        self.verify_host_key = bool(verify_host_key)
        self.known_hosts = known_hosts or ""
        self._transport = None
        self._channel = None
        self.prompt = ">"

    def connect(self):
        try:
            import paramiko
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise RuntimeError("missing_paramiko") from exc

        sock = socket.create_connection((self.host, self.port), self.timeout)
        sock.settimeout(self.timeout)
        transport = paramiko.Transport(sock)
        transport.banner_timeout = self.timeout
        transport.auth_timeout = self.timeout
        try:
            transport.start_client(timeout=self.timeout)
        except paramiko.SSHException as exc:
            transport.close()
            raise RuntimeError("ssh_error") from exc

        if self.verify_host_key:
            self._verify_host_key(transport, paramiko)

        try:
            # Preferred path for DrayOS: transport auth succeeds without a
            # password, then the CLI itself asks for the credentials.
            transport.auth_none(self.username)
        except paramiko.BadAuthenticationType as exc:
            allowed = tuple(getattr(exc, "allowed_types", ()) or ())
            if "password" not in allowed:
                transport.close()
                raise RuntimeError("auth_error") from exc
            try:
                transport.auth_password(self.username, self.password)
            except paramiko.AuthenticationException as auth_exc:
                transport.close()
                raise RuntimeError("auth_error") from auth_exc
        except paramiko.AuthenticationException as exc:
            transport.close()
            raise RuntimeError("auth_error") from exc

        if not transport.is_authenticated():
            transport.close()
            raise RuntimeError("auth_error")

        try:
            channel = transport.open_session(timeout=self.timeout)
            channel.get_pty(term="vt100", width=200, height=1000)
            channel.settimeout(self.timeout)
            channel.invoke_shell()
        except paramiko.SSHException as exc:
            transport.close()
            raise RuntimeError("ssh_error") from exc
        self._transport = transport
        self._channel = channel
        try:
            self._login()
        except Exception:
            self.close()
            raise

    def _verify_host_key(self, transport, paramiko):
        try:
            from paramiko.hostkeys import HostKeys
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("host_key_error") from exc
        candidates = [self.known_hosts] if self.known_hosts else []
        candidates.append(os.path.expanduser("~/.ssh/known_hosts"))
        hostkeys = HostKeys()
        loaded = False
        for path in candidates:
            if path and os.path.exists(path):
                try:
                    hostkeys.load(path)
                except Exception:  # malformed or unreadable known_hosts entry
                    continue
                loaded = True
        if not loaded:
            raise RuntimeError("host_key_error")
        server_key = transport.get_remote_server_key()
        for name in (self.host, f"[{self.host}]:{self.port}"):
            entry = hostkeys.lookup(name)
            if entry and entry.get(server_key.get_name()) == server_key:
                return
        raise RuntimeError("host_key_error")

    def _decode(self, data):
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("latin-1", "replace")

    def _send(self, text):
        if self._channel is None:
            raise RuntimeError("connection_error")
        self._channel.sendall((text + "\n").encode("utf-8"))

    def _read_until(self, patterns, timeout, require_end=True):
        deadline = time.monotonic() + timeout
        buffer = ""
        while time.monotonic() < deadline:
            if self._channel is None:
                break
            if self._channel.recv_ready():
                chunk = self._channel.recv(65536)
                if not chunk:
                    break
                buffer += self._decode(chunk)
                buffer = _strip_ansi(buffer)
                if _PAGER_RE.search(buffer):
                    try:
                        self._channel.sendall(b" ")
                    except OSError:
                        pass
                    buffer = _PAGER_RE.sub("", buffer)
                for pattern in patterns:
                    match = pattern.search(buffer)
                    if match and (not require_end or not buffer[match.end() :].strip()):
                        return buffer, match
            elif self._channel.closed:
                break
            else:
                time.sleep(0.05)
        raise TimeoutError("timeout")

    def _login(self):
        username_re = re.compile(r"(?i)username\s*:\s*$")
        password_re = re.compile(r"(?i)password\s*:\s*$")
        denied_re = re.compile(r"(?i)access\s+denied")
        buffer, _match = self._read_until(
            (_PROMPT_RE, username_re, denied_re), self.timeout, require_end=False
        )
        if denied_re.search(buffer):
            raise RuntimeError("auth_error")

        if username_re.search(buffer):
            self._send(self.username)
            buffer, _match = self._read_until(
                (password_re, _PROMPT_RE, denied_re),
                self.timeout,
                require_end=False,
            )
            if denied_re.search(buffer):
                raise RuntimeError("auth_error")

        if password_re.search(buffer):
            self._send(self.password)
            buffer, match = self._read_until(
                (_PROMPT_RE, denied_re, username_re),
                self.timeout,
                require_end=False,
            )
            if denied_re.search(buffer) or username_re.search(buffer):
                raise RuntimeError("auth_error")
            if not _PROMPT_RE.search(buffer):
                raise RuntimeError("auth_error")
            self.prompt = match.group(0).strip() if match else ">"
            return

        if not _PROMPT_RE.search(buffer):
            raise RuntimeError("auth_error")
        match = _PROMPT_RE.search(buffer)
        self.prompt = match.group(0).strip() if match else ">"

    def command(self, command, timeout=None):
        timeout = self.timeout if timeout is None else timeout
        # Discard a pending prompt or late banner data before sending.
        while self._channel is not None and self._channel.recv_ready():
            self._channel.recv(65536)
        self._send(command)
        try:
            buffer, _match = self._read_until((_PROMPT_RE,), timeout, require_end=True)
        except TimeoutError:
            # Return the CLI to its prompt so a fallback command can still run.
            try:
                self._channel.sendall(b"\x03")
            except OSError:
                pass
            raise
        return _clean_cli_output(buffer, command, self.prompt)

    def close(self):
        channel = self._channel
        self._channel = None
        if channel is not None:
            try:
                channel.sendall(b"quit\n")
            except OSError:
                pass
            try:
                channel.close()
            except OSError:
                pass
        transport = self._transport
        self._transport = None
        if transport is not None:
            try:
                transport.close()
            except OSError:
                pass


def _as_bool(value, default=True):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}


def _merge_parsed(target, extra):
    """Fill missing fields from a second CLI page without overwriting values."""
    for key, value in extra.items():
        if key == "counter_direction":
            continue
        if key not in target or target[key] in (None, "", []):
            target[key] = value
        elif isinstance(target[key], list) and isinstance(value, list):
            for index, item in enumerate(value):
                if index < len(target[key]):
                    if target[key][index] is None and item is not None:
                        target[key][index] = item
                elif item is not None:
                    target[key].append(item)
    if extra.get("counter_direction") and any(key in extra for key in COUNTER_BASE):
        target["counter_direction"] = extra["counter_direction"]


def _run_error(state, state_file, start, error):
    state["scrape_fail_count"] = state.get("scrape_fail_count", 0) + 1
    _save_state(state_file, state)
    print(
        json.dumps(
            {
                "vigor_dsl_scrape_success": 0,
                "vigor_dsl_error": error,
                "vigor_dsl_scrape_duration_seconds": round(time.time() - start, 3),
            }
        )
    )
    # Signal the failed run through the process exit code so the agent records
    # `agent:error=1` (generic rule rbfprf92 alarms on it).  The payload above
    # still lands as metrics because the agent keeps valid JSON printed by a
    # non-zero exit, so scrape_success/error stay visible to rules/dashboards.
    sys.exit(1)


def run(config):
    host = str(config.get("host") or "").strip()
    password = config.get("password") or ""
    if not host or not password:
        print(
            json.dumps(
                {"vigor_dsl_scrape_success": 0, "vigor_dsl_error": "missing_config"}
            )
        )
        sys.exit(1)
    try:
        port = int(config.get("ssh_port", DEFAULT_SSH_PORT))
        timeout = int(config.get("timeout", 10))
    except (TypeError, ValueError):
        print(
            json.dumps(
                {"vigor_dsl_scrape_success": 0, "vigor_dsl_error": "invalid_config"}
            )
        )
        sys.exit(1)

    state_file = config.get("state_file") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".vigor167_state.json"
    )
    state = _load_state(state_file)
    started = time.time()
    client = Vigor167SSHClient(
        host,
        port=port,
        username=str(config.get("username") or "admin"),
        password=password,
        timeout=timeout,
        verify_host_key=_as_bool(config.get("verify_host_key"), False),
        known_hosts=str(config.get("known_hosts") or "").strip(),
    )

    parsed = {}
    monitoring_ok = False
    timeout_seen = False
    try:
        client.connect()
        summary_command = str(config.get("dsl_command") or DEFAULT_DSL_COMMAND).strip()
        if summary_command:
            try:
                summary = parse_cli_output(
                    client.command(summary_command, timeout=timeout)
                )
                _merge_parsed(parsed, summary)
            except TimeoutError:
                timeout_seen = True

        # Classic DrayOS 4 modems expose the same data through "vdsl status".
        # Only try these when the primary command did not return a line state.
        if not parsed.get("line_state"):
            for command in (
                "vdsl status more",
                "vdsl status",
                "adsl status more",
                "adsl status",
            ):
                try:
                    candidate = parse_cli_output(
                        client.command(command, timeout=timeout)
                    )
                except TimeoutError:
                    timeout_seen = True
                    continue
                _merge_parsed(parsed, candidate)
                if parsed.get("line_state") and any(
                    key in parsed for key in COUNTER_BASE
                ):
                    break

        # DrayOS 5's exec dslinfo is a summary.  The monitoring page exposes
        # the stream/error tables as the same JSON the web API uses; enter it
        # read-only, print it with "show all" and close the session afterwards.
        if (
            not any(key in parsed for key in COUNTER_BASE)
            or "attenuation" not in parsed
        ):
            try:
                client.command(MONITORING_PAGE_COMMAND, timeout=timeout)
                monitoring_text = client.command(
                    MONITORING_SHOW_COMMAND, timeout=timeout
                )
                _merge_parsed(parsed, parse_monitoring_json(monitoring_text))
            except TimeoutError:
                timeout_seen = True
            except (OSError, RuntimeError):
                # The optional counter page must not discard a valid summary.
                pass

        monitoring_ok = bool(parsed.get("line_state"))
    except RuntimeError as exc:
        # The client only raises fixed codes, never payload content.
        _run_error(state, state_file, started, str(exc) or "cli_error")
        return
    except TimeoutError:
        _run_error(state, state_file, started, "timeout_error")
        return
    except OSError:
        _run_error(state, state_file, started, "connection_error")
        return
    finally:
        client.close()

    if not parsed or not parsed.get("line_state"):
        _run_error(
            state,
            state_file,
            started,
            "timeout_error" if timeout_seen else "parse_error",
        )
        return

    metrics = compute_poll(state, parsed)
    metrics["vigor_dsl_scrape_duration_seconds"] = round(time.time() - started, 3)
    metrics["vigor_dsl_monitoring_success"] = int(monitoring_ok)
    if parsed.get("counter_direction"):
        # Keep both names during the migration from vigor130.
        metrics["vigor_dsl_counter_columns"] = parsed["counter_direction"]
        metrics["vigor_dsl_counter_direction"] = parsed["counter_direction"]
    _save_state(state_file, state)
    print(json.dumps(metrics))


if __name__ == "__main__":
    try:
        configuration = json.load(sys.stdin)
    except (ValueError, TypeError):
        configuration = {}
    try:
        run(configuration)
    except Exception:
        print(
            json.dumps({"vigor_dsl_scrape_success": 0, "vigor_dsl_error": "internal"})
        )
        sys.exit(1)
