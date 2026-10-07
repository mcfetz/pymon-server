#!/usr/bin/env python3
"""DrayTek Vigor 167 DSL monitoring in bridge mode.

The Vigor 167 uses the DrayOS 5 JSON/CGI API rather than the legacy Vigor 130
HTML pages.  This collector only performs authenticated read requests:
``event/552`` for login and ``CFG_MULTI_GET/501`` for status PIDs.

Credentials, CGI payloads, cookies and tokens never enter metrics or errors.
"""

import base64
import hashlib
import http.cookiejar
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


__schema__ = {
    "label": "DrayTek Vigor 167 (DSL)",
    "description": "DrayTek Vigor 167 DSL modem monitoring (bridge mode)",
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
            "key": "port",
            "label": "Web port (80=http, 443=https)",
            "type": "number",
            "default": 80,
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
            "key": "http_encode",
            "label": "Use DrayOS CT encoding",
            "type": "boolean",
            "default": True,
        },
        {
            "key": "verify_tls",
            "label": "Verify HTTPS certificates",
            "type": "boolean",
            "default": True,
        },
        {
            "key": "ca_file",
            "label": "HTTPS CA file (optional)",
            "type": "string",
            "default": "",
            "optional": True,
        },
        {
            "key": "timeout",
            "label": "HTTP timeout (s)",
            "type": "number",
            "default": 8,
            "min": 3,
            "max": 15,
        },
        # Request line of the DrayOS 5 web UI. Firmware revisions differ, so the
        # three values stay editable: correct them from a browser dev-tools
        # capture instead of editing the plugin.
        {
            "key": "cgi_path",
            "label": "CGI path",
            "type": "string",
            "default": "/cgi-bin/webproc.cgi",
        },
        {
            "key": "login_op",
            "label": "Login op code",
            "type": "string",
            "default": "552",
        },
        {
            "key": "status_op",
            "label": "Status op code",
            "type": "string",
            "default": "501",
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

DEFAULT_CGI_PATH = "/cgi-bin/webproc.cgi"
DEFAULT_LOGIN_OP = "552"
DEFAULT_STATUS_OP = "501"

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
    "fecs": "fecs",
    "es": "es",
    "ses": "ses",
    "loss": "loss",
    "uas": "uas",
    "hec": "hec_errors",
    "hec errors": "hec_errors",
    "hec_error": "hec_errors",
    "rs corrections": "rs_corrections",
    "rs correction": "rs_corrections",
    "los failure": "los_failure",
    "lof failure": "lof_failure",
    "lpr failure": "lpr_failure",
    "ncd failure": "ncd_failure",
    "lcd failure": "lcd_failure",
    "nfec": "nfec",
    "rfec": "rfec",
    "lysmb": "lysmb",
    "attenuation": "attenuation",
}
STREAM_ALIASES = {
    "actual rate": "actual_rate",
    "actual line rate": "actual_rate",
    "attainable rate": "attainable_rate",
    "attainable line rate": "attainable_rate",
    "snr margin": "snr_margin",
    "snr": "snr_margin",
    "attenuation": "attenuation",
    "path mode": "path_mode",
    "interleave depth": "interleave_depth",
}
SYNC_METRICS = {
    "actual_rate": "sync_rate_kbps",
    "attainable_rate": "attainable_rate_kbps",
    "snr_margin": "snr_db",
    "attenuation": "attenuation_db",
    "interleave_depth": "interleave_depth",
}
STATE_VERSION = 1


def _norm(value):
    return re.sub(r"\s+", " ", str(value or "").strip(" .:_-\t\r\n")).lower()


def _san(value):
    value = str(value or "").strip()
    value = re.sub(r"\s+", "_", value)
    return re.sub(r"[^A-Za-z0-9_.\-/]", "", value)


def _to_num(value):
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in PLACEHOLDERS:
        return None
    match = re.search(r"[-+]?\d+(?:[,.]\d+)?", text)
    if not match:
        return None
    try:
        number = float(match.group(0).replace(",", "."))
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


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


def _pair(downstream, upstream):
    return [_to_num(downstream), _to_num(upstream)]


def _pair_text(first, second):
    values = []
    for value in (first, second):
        text = str(value or "").strip()
        values.append(None if text.lower() in PLACEHOLDERS else text)
    return values


def _first_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return item
    return None


def _walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _response_ok(response):
    return (
        isinstance(response, dict)
        and str(response.get("rid", ""))[:4] == "0000"
        and isinstance(response.get("ct"), list)
        and bool(response.get("ct"))
    )


def _merge_responses(*responses):
    """Combine successful PID responses without retaining transport metadata."""
    merged = {"rid": "2001", "ct": []}
    successful = False
    for response in responses:
        if not _response_ok(response):
            continue
        successful = True
        blocks = response.get("ct")
        if isinstance(blocks, list):
            merged["ct"].extend(blocks)
    if successful:
        merged["rid"] = "0000"
    return merged


def _find_blocks(response, names):
    wanted = set(names)
    found = []
    for item in _walk_dicts(response):
        for key, value in item.items():
            if key in wanted:
                found.append(value)
    return found


def _find_first_row(response, names):
    for block in _find_blocks(response, names):
        row = _first_dict(block)
        if row:
            return row
    return None


def _row_name(row):
    return _norm(
        row.get("Name") or row.get("name") or row.get("Title") or row.get("title")
    )


def _counter_key(name):
    normal = _norm(name).replace("_", " ")
    return COUNTER_ALIASES.get(normal)


def _stream_key(name):
    normal = _norm(name).replace("_", " ")
    return STREAM_ALIASES.get(normal)


def _rows_with_keys(response, names):
    rows = []
    for block in _find_blocks(response, names):
        for row in _walk_dicts(block):
            if isinstance(row, dict) and _row_name(row):
                rows.append(row)
    return rows


def parse_api_responses(
    status_response, monitoring_response=None, system_response=None
):
    """Convert Vigor 167 CGI responses into the collector's neutral model."""
    if not _response_ok(status_response):
        return {}

    # Firmware revisions either return the general monitoring tree together
    # with the status PID or as a separate PID response.  Accept both shapes.
    monitoring_source = monitoring_response or {}
    if not _find_blocks(
        monitoring_source,
        ("0MONITORING_DSL_GENERAL", "1MON_DSL_STREAM_TABLE", "1MON_DSL_END_TABLE"),
    ):
        monitoring_source = status_response

    status = _find_first_row(status_response, ("1DSL_STS_INFO_LITE",)) or {}
    if not status:
        status = _find_first_row(monitoring_source, ("0MONITORING_DSL_GENERAL",)) or {}
    line_state = str(status.get("Status", "")).strip()
    if not line_state:
        return {}

    parsed = {
        "line_state": _canonical_state(line_state),
        "mode": str(status.get("Mode", "")).strip(),
        "profile": str(status.get("Profile", "")).strip(),
        "annex": str(status.get("Annex", "")).strip(),
        "dsl_version": str(status.get("DSL_Version", "")).strip(),
        "line_uptime_seconds": _to_duration(status.get("Line_Uptime")),
        "actual_rate": _pair(
            status.get("Downstream_Line_Rate"), status.get("Upstream_Line_Rate")
        ),
        "snr_margin": _pair(status.get("SNR_Downstream"), status.get("SNR_Upstream")),
    }
    attenuation = _to_num(status.get("Downstream_Line_Attenuation"))
    if attenuation is not None:
        parsed["attenuation"] = [attenuation, None]

    # The general monitoring PID can duplicate status fields and carries the
    # stream/end table.  Prefer it only where the status PID has no value.
    general = _find_first_row(monitoring_source, ("0MONITORING_DSL_GENERAL",)) or {}
    for key, source in (
        ("mode", "Mode"),
        ("profile", "Profile"),
        ("annex", "Annex"),
        ("dsl_version", "DSL_Version"),
    ):
        if not parsed.get(key) and general.get(source):
            parsed[key] = str(general[source]).strip()

    stream_rows = _rows_with_keys(monitoring_source, ("1MON_DSL_STREAM_TABLE",))
    end_rows = _rows_with_keys(monitoring_source, ("1MON_DSL_END_TABLE",))
    # Some firmware versions nest the rows only under the general PID.
    stream_rows += [
        row
        for row in _walk_dicts(general.get("Stream_Table", []))
        if isinstance(row, dict) and _row_name(row)
    ]
    end_rows += [
        row
        for row in _walk_dicts(general.get("End_Table", []))
        if isinstance(row, dict) and _row_name(row)
    ]

    for row in stream_rows:
        key = _stream_key(_row_name(row))
        if key:
            pair = (
                _pair_text(row.get("Downstream"), row.get("Upstream"))
                if key == "path_mode"
                else _pair(row.get("Downstream"), row.get("Upstream"))
            )
            if any(value is not None for value in pair):
                parsed[key] = pair
        counter = _counter_key(_row_name(row))
        if counter and counter not in parsed:
            pair = _pair(row.get("Downstream"), row.get("Upstream"))
            if any(value is not None for value in pair):
                parsed[counter] = pair

    for row in end_rows:
        key = _counter_key(_row_name(row))
        if not key:
            continue
        pair = _pair(row.get("Near_End"), row.get("Far_End"))
        if any(value is not None for value in pair):
            if key == "attenuation":
                parsed["attenuation"] = pair
            else:
                # The dedicated Near/Far table is authoritative over a
                # stream-table duplicate because it preserves the modem's
                # actual error-counter semantics.
                parsed[key] = pair

    parsed["counter_direction"] = (
        "near_end_far_end" if end_rows else "downstream_upstream"
    )
    system = _find_first_row(system_response or {}, ("1SYSTEM_INFO",))
    if system:
        parsed["system_uptime_seconds"] = _to_duration(system.get("System_Uptime"))
    return {key: value for key, value in parsed.items() if value not in (None, "")}


def _canonical_state(value):
    text = re.sub(r"[^A-Za-z0-9]+", "", str(value or "")).lower()
    if text.endswith("showtime") or text == "showtime":
        return "SHOWTIME"
    if text.endswith("training") or text == "training":
        return "TRAINING"
    return _san(value).upper()


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
# DrayOS 5 JSON/CGI transport
# ---------------------------------------------------------------------------


def _encode_uri(value):
    # JavaScript encodeURI's unescaped character set.
    return urllib.parse.quote(value, safe=";/?:@&=+$-_.!~*'()#")


def build_login_payload(username, password, utc=None, locales="en"):
    if utc is None:
        utc = int(time.time())
    return {
        "param": [],
        "ct": [
            {
                "Name": username,
                "Password": hashlib.sha512(password.encode()).hexdigest(),
                "utc": utc,
                "VaildationCode": "",
                "locales": locales,
            }
        ],
    }


def _encode_ct(value):
    """Encode JSON with DrayOS 5's padding-count/base64 CT wrapper."""
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    padding = len(encoded) - len(encoded.rstrip("="))
    return f"{padding}{encoded[:-padding] if padding else encoded}"


def build_cgi_body(pid, op, payload, http_encode=True):
    content = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    ct = _encode_ct(content) if http_encode else _encode_uri(content)
    return f"pid={pid}&op={op}&ct={ct}"


def decode_cgi_response(text):
    """Decode standalone JSON or DrayOS CT-prefixed base64 JSON."""
    text = (text or "").strip()
    if text.startswith("{") or text.startswith("["):
        return json.loads(text)
    if not text or not text[0].isdigit() or int(text[0]) > 2:
        raise ValueError("invalid CGI response")
    padding = int(text[0])
    raw = base64.b64decode(text[1:] + "=" * padding)
    return json.loads(raw.decode("utf-8"))


class Vigor167Client:
    """Cookie-aware, read-only Vigor 167 API client.

    The request line (CGI path plus op codes) is configurable because it is
    firmware specific; the defaults are what the DrayOS 5 web UI itself sends.
    """

    def __init__(
        self,
        base_url,
        timeout=8,
        cgi_path=DEFAULT_CGI_PATH,
        login_op=DEFAULT_LOGIN_OP,
        status_op=DEFAULT_STATUS_OP,
        http_encode=True,
        verify_tls=True,
        ca_file="",
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.http_encode = bool(http_encode)
        path = str(cgi_path or DEFAULT_CGI_PATH).strip()
        self.cgi_path = path if path.startswith("/") else f"/{path}"
        self.login_op = str(login_op or DEFAULT_LOGIN_OP)
        self.status_op = str(status_op or DEFAULT_STATUS_OP)
        if verify_tls:
            context = ssl.create_default_context(cafile=ca_file or None)
        else:
            # Insecure TLS is an explicit opt-in for devices with an
            # untrusted certificate. Prefer HTTP on the isolated management
            # VLAN or provide the modem's CA file instead.
            context = ssl._create_unverified_context()
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context),
            urllib.request.HTTPCookieProcessor(jar),
        )

    def _post(self, body, content_type=None):
        if content_type is None:
            content_type = (
                "text/plain; charset=utf-8"
                if self.http_encode
                else "application/json; charset=utf-8"
            )
        request = urllib.request.Request(
            f"{self.base_url}{self.cgi_path}",
            data=body.encode("utf-8"),
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": content_type,
                "User-Agent": "pymon-vigor167/1.0",
            },
            method="POST",
        )
        with self.opener.open(request, timeout=self.timeout) as response:
            return decode_cgi_response(response.read().decode("utf-8", "replace"))

    def login(self, username, password):
        """Authenticate; raises ``RuntimeError("auth_error")`` when rejected."""
        # The standalone web UI sends this first request with its JSON content
        # type; the CT wrapper is controlled by the HTTP_Encode setting.
        response = self._post(
            build_cgi_body(
                "event",
                self.login_op,
                build_login_payload(username, password),
                self.http_encode,
            ),
            content_type="application/json; charset=utf-8",
        )
        if not _response_ok(response):
            raise RuntimeError("auth_error")
        return response

    def get_pid(self, pid):
        """Read one status PID; raises ``RuntimeError("cgi_error")`` on refusal."""
        payload = {"param": [], "ct": [{pid: []}]}
        response = self._post(
            build_cgi_body(pid, self.status_op, payload, self.http_encode)
        )
        if not _response_ok(response):
            raise RuntimeError("cgi_error")
        return response


def _as_bool(value, default=True):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}


def _base_url(host, port):
    scheme = "https" if int(port) in (443, 11443) else "http"
    return f"{scheme}://{host}:{int(port)}"


def _optional_pid(client, pid):
    """Read an auxiliary PID without turning missing optional data into down."""
    try:
        return client.get_pid(pid)
    except (OSError, RuntimeError, TimeoutError, ValueError, urllib.error.HTTPError):
        return {}


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


def run(config):
    host = str(config.get("host") or "").strip()
    password = config.get("password") or ""
    if not host or not password:
        print(
            json.dumps(
                {"vigor_dsl_scrape_success": 0, "vigor_dsl_error": "missing_config"}
            )
        )
        return
    try:
        port = int(config.get("port", 80))
        timeout = int(config.get("timeout", 8))
    except (TypeError, ValueError):
        print(
            json.dumps(
                {"vigor_dsl_scrape_success": 0, "vigor_dsl_error": "invalid_config"}
            )
        )
        return

    state_file = config.get("state_file") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".vigor167_state.json"
    )
    state = _load_state(state_file)
    started = time.time()
    monitoring_pids = (
        "0MONITORING_DSL_GENERAL",
        "1MON_DSL_STREAM_TABLE",
        "1MON_DSL_END_TABLE",
    )
    monitoring_pids_result = []
    client = Vigor167Client(
        _base_url(host, port),
        timeout=timeout,
        cgi_path=config.get("cgi_path"),
        login_op=config.get("login_op"),
        status_op=config.get("status_op"),
        http_encode=_as_bool(config.get("http_encode"), True),
        verify_tls=_as_bool(config.get("verify_tls"), True),
        ca_file=str(config.get("ca_file") or "").strip(),
    )
    try:
        client.login(str(config.get("username") or "admin"), password)
        status = client.get_pid("1DSL_STS_INFO_LITE")
        monitoring_pids_result = [_optional_pid(client, pid) for pid in monitoring_pids]
        monitoring = _merge_responses(*monitoring_pids_result)
        system = _optional_pid(client, "1SYSTEM_INFO")
    except urllib.error.HTTPError as exc:
        # Only 401/403 mean "credentials rejected": a 404 from a wrong CGI path
        # must not masquerade as an auth problem.
        error = "auth_error" if exc.code in (401, 403) else f"http_{exc.code}"
        _run_error(state, state_file, started, error)
        return
    except (TimeoutError, OSError):
        _run_error(state, state_file, started, "connection_error")
        return
    except RuntimeError as exc:
        # The client only ever raises fixed codes, never payload content.
        _run_error(state, state_file, started, str(exc) or "cgi_error")
        return
    except ValueError:
        _run_error(state, state_file, started, "cgi_error")
        return

    parsed = parse_api_responses(status, monitoring, system)
    if not parsed:
        _run_error(state, state_file, started, "parse_error")
        return

    metrics = compute_poll(state, parsed)
    metrics["vigor_dsl_scrape_duration_seconds"] = round(time.time() - started, 3)
    metrics["vigor_dsl_monitoring_success"] = int(
        all(_response_ok(response) for response in monitoring_pids_result)
    )
    metrics["vigor_dsl_system_info_success"] = int(bool(_response_ok(system)))
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
