#!/usr/bin/env python3
"""vigor130.py — DrayTek Vigor 130 DSL modem monitoring (bridge mode).

Fetches the authenticated DSL status page, parses line/counter values and
keeps a small persistent state file so that counter deltas, resync/reboot
detection, baseline handling and outage tracking survive agent restarts.

Credentials and session tokens are never emitted into metric names, stdout
or error strings.
"""
import http.cookiejar
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from html import unescape

__schema__ = {
    "label": "DrayTek Vigor 130 (DSL)",
    "description": "DrayTek Vigor 130 DSL modem monitoring (bridge mode)",
    "fields": [
        {"key": "sleep", "label": "Interval (s)", "type": "number", "default": 60, "min": 30},
        {"key": "host", "label": "Vigor hostname or IP", "type": "string", "default": "192.168.1.1"},
        {"key": "port", "label": "Web port (11080=http, 11443=https)", "type": "number", "default": 11080, "min": 1, "max": 65535},
        {"key": "username", "label": "Vigor admin username", "type": "string", "default": "admin"},
        {"key": "password", "label": "Vigor admin password", "type": "string"},
        {"key": "timeout", "label": "HTTP timeout (s)", "type": "number", "default": 8, "min": 3, "max": 15},
        {"key": "state_file", "label": "State file (optional)", "type": "string", "default": "", "optional": True},
    ],
}

PLACEHOLDERS = {"---", "[---]", "(nil)", "-", "", "n/a"}

# (state_key, page_label, value_kind)
# kind: str | num | pair | pair_num | pair_str
DSL_FIELDS = [
    ("line_state", "Line State", "str"),
    ("running_mode", "Running Mode", "str"),
    ("type", "Type", "str"),
    ("actual_rate", "Actual Rate", "pair_num"),
    ("attainable_rate", "Attainable Rate", "pair_num"),
    ("path_mode", "Path Mode", "pair_str"),
    ("interleave_depth", "Interleave Depth", "pair_num"),
    ("snr_margin", "SNR Margin", "pair_num"),
    ("attenuation", "Attenuation", "pair_num"),
    ("crc", "CRC", "pair_num"),
    ("fecs", "FECS", "pair_num"),
    ("es", "ES", "pair_num"),
    ("ses", "SES", "pair_num"),
    ("loss", "LOSS", "pair_num"),
    ("uas", "UAS", "pair_num"),
    ("hec_errors", "HEC Errors", "pair_num"),
    ("rs_corrections", "RS Corrections", "pair_num"),
    ("los_failure", "LOS Failure", "pair_num"),
    ("lof_failure", "LOF Failure", "pair_num"),
    ("lpr_failure", "LPR Failure", "pair_num"),
    ("ncd_failure", "NCD Failure", "pair_num"),
    ("lcd_failure", "LCD Failure", "pair_num"),
    ("nfec", "NFEC", "pair_num"),
    ("rfec", "RFEC", "pair_num"),
    ("lysmb", "LYSMB", "pair_num"),
]

FIELD_KIND = {k: kind for k, _label, kind in DSL_FIELDS}


def _norm_label(label: str) -> str:
    return re.sub(r"\s+", " ", label.strip(" .:")).lower()


LABEL_KEYS = {_norm_label(label): key for key, label, _kind in DSL_FIELDS}


# Scale/unit/label of each counter. Value is the absolute counter read from the
# device; delta and per-hour rate are derived in the state machine.
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

# Non-counter numeric values exposed directly each poll (downstream/upstream).
SYNC_METRICS = {
    "actual_rate": "sync_rate_kbps",
    "attainable_rate": "attainable_rate_kbps",
    "snr_margin": "snr_db",
    "attenuation": "attenuation_db",
    "interleave_depth": "interleave_depth",
}

STATE_VERSION = 1


def _to_num(value):
    """Parse a numeric token, tolerating comma or dot decimals and units."""
    if value is None:
        return None
    s = str(value).strip()
    if s.lower() in PLACEHOLDERS:
        return None
    m = re.search(r"[-+]?\d+(?:[,.]\d+)?", s)
    if not m:
        return None
    token = m.group(0).replace(",", ".")
    try:
        f = float(token)
        return int(f) if f.is_integer() else f
    except ValueError:
        return None


def _split_pair(text):
    """Split 'A / B' or 'A downstream / B upstream' into two raw parts."""
    if text is None:
        return None
    text = str(text).strip()
    if "downstream" in text.lower() or "upstream" in text.lower():
        m = re.search(r"(?is)^(.*?)\s+downstream\s*/\s*(.*?)\s+upstream\s*$", text)
        if m:
            return [m.group(1).strip(), m.group(2).strip()]
    parts = [p.strip() for p in re.split(r"/", text)]
    if len(parts) >= 2:
        return [parts[0], parts[1]]
    return [text]


def _parse_value(raw, kind):
    """Convert a raw page value string into typed data."""
    if kind == "str":
        return raw.strip()
    if kind == "num":
        return _to_num(raw)
    if kind in ("pair", "pair_num", "pair_str"):
        parts = _split_pair(raw)
        if not parts:
            return None
        if kind == "pair_num":
            return [_to_num(p) for p in parts]
        return parts
    return raw


def _is_valid(value):
    """True if a parsed value is usable (not empty, placeholder or all-None pair)."""
    if value is None:
        return False
    if isinstance(value, (list, tuple)):
        return bool(value) and any(v is not None for v in value)
    if isinstance(value, str):
        return bool(value.strip()) and value.strip().lower() not in PLACEHOLDERS
    return True


def _normalize_text(html):
    """Convert HTML into a list of meaningful text lines."""
    html = re.sub(r"(?is)<(script|style|textarea)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</(td|th|tr|div|p|li|h[123456])>", "\n", html)
    html = re.sub(r"<[^>]+>", " ", html)
    text = unescape(html)
    lines = [" ".join(l.split()) for l in text.splitlines()]
    return [l for l in lines if l]


def parse_dsl_page(html):
    """Extract typed DSL fields from the dslstatus page.

    Returns dict keyed by field key; missing/unparseable fields are omitted.
    """
    lines = _normalize_text(html)
    found = {}

    for line in lines:
        m = re.match(r"^([A-Za-z][\w .()\-]{0,40}?):\s*(.*)$", line)
        if not m:
            continue
        key = LABEL_KEYS.get(_norm_label(m.group(1)))
        if key and key not in found:
            value = _parse_value(m.group(2), FIELD_KIND[key])
            if _is_valid(value):
                found[key] = value

    # Two-column table layout: 'Label' line followed by a value line.
    for i, line in enumerate(lines):
        key = LABEL_KEYS.get(_norm_label(line))
        if not key or key in found or i + 1 >= len(lines):
            continue
        nxt = lines[i + 1]
        if (
            _to_num(nxt) is not None
            or "downstream" in nxt.lower()
            or _norm_label(nxt) not in LABEL_KEYS
        ):
            value = _parse_value(nxt, FIELD_KIND[key])
            if _is_valid(value):
                found[key] = value

    return found


def _san(str_value):
    """Make an arbitrary string safe as a metric value."""
    s = str(str_value).strip()
    if not s:
        return ""
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^A-Za-z0-9_.\-/]", "", s)
    return s


def _pair_metrics(prefix, pair, out, suffix=("_downstream", "_upstream")):
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        return
    for val, suf in zip(pair, suffix):
        if val is not None:
            out[f"vigor_dsl_{prefix}{suf}"] = val


def _fresh_state():
    now = time.time()
    return {
        "_version": STATE_VERSION,
        "baseline": False,
        "last_poll_at": None,
        "last_line_state": None,
        "last_uptime": None,
        "counters": {},
        "last_counter_ts": None,
        "last_showtime_loss_at": None,
        "last_outage_seconds": None,
        "resync_count": 0,
        "reboot_count": 0,
        "scrape_fail_count": 0,
        "created_at": now,
    }


def _load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            return _fresh_state()
        for k, v in _fresh_state().items():
            state.setdefault(k, v)
        state["_version"] = STATE_VERSION
        return state
    except (OSError, ValueError):
        return _fresh_state()


def _save_state(path, state):
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def _default_state_file():
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(plugin_dir, ".vigor130_state.json")


def compute_poll(state, parsed, now=None, uptime_seconds=None):
    """Run the state machine for one successful scrape.

    Mutates ``state`` in place and returns the metrics dict for this poll.
    """
    if now is None:
        now = time.time()
    metrics = {}
    line_state = parsed.get("line_state")
    ok_state = bool(line_state and str(line_state).upper() not in PLACEHOLDERS)
    metrics["vigor_dsl_scrape_success"] = 1 if ok_state else 0
    if not ok_state:
        state["scrape_fail_count"] = state.get("scrape_fail_count", 0) + 1
        return metrics

    state["scrape_fail_count"] = 0
    state["last_poll_at"] = now
    state_key = str(line_state).upper().strip()
    metrics["vigor_dsl_line_state"] = _san(state_key)
    line_up = 1 if state_key == "SHOWTIME" else 0

    for key, label in (("type", "type"), ("running_mode", "running_mode")):
        if parsed.get(key) is not None:
            metrics[f"vigor_dsl_{label}"] = _san(parsed[key])

    if uptime_seconds is not None:
        try:
            up = float(uptime_seconds)
            metrics["vigor_dsl_uptime_seconds"] = int(up)
        except (TypeError, ValueError):
            up = None
    else:
        up = None

    for key, prefix in SYNC_METRICS.items():
        _pair_metrics(prefix, parsed.get(key), metrics)

    last = state.get("last_line_state")
    loss_at = state.get("last_showtime_loss_at")
    was_baseline = bool(state.get("baseline"))

    if not was_baseline:
        # First successful poll: only establish the baseline. No transition
        # logic runs and no numeric line_up value is emitted, so the first
        # scrape can never trigger a DSL-down rule by itself.
        state["baseline"] = True
    else:
        if last == "SHOWTIME" and state_key != "SHOWTIME":
            if not loss_at:
                state["last_showtime_loss_at"] = now
        elif last != "SHOWTIME" and state_key == "SHOWTIME" and loss_at:
            state["last_outage_seconds"] = max(0.0, now - loss_at)
            metrics["vigor_dsl_last_outage_seconds"] = round(state["last_outage_seconds"], 1)
            state["last_showtime_loss_at"] = None

    if was_baseline:
        metrics["vigor_dsl_line_up"] = line_up

    state["last_line_state"] = state_key

    # ---- counters: absolute, delta and per-hour rate ----
    # Capture previous counter reads BEFORE overwriting them, so counter-reset
    # detection uses the old values.
    counter_ts = state.get("last_counter_ts")
    elapsed = (now - counter_ts) if (counter_ts and now >= counter_ts) else None
    prev_reads = {}
    for key, base in COUNTER_BASE.items():
        pair = parsed.get(key)
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        for val, direction in zip(pair, ("_downstream", "_upstream")):
            if val is None:
                continue
            ckey = f"{base}{direction}"
            prev_reads[ckey] = (val, state.get("counters", {}).get(ckey))

    any_reset = False
    for ckey, (val, prev) in prev_reads.items():
        base = ckey.rsplit("_", 1)[0]
        direction = "_downstream" if ckey.endswith("_downstream") else "_upstream"
        metrics[f"vigor_dsl_{base}_total{direction}"] = val
        state.setdefault("counters", {})[ckey] = val
        if prev is None:
            continue
        delta = val - prev
        if delta < -1e-6:
            # Counter reset (resync/reboot): never forward a negative rate.
            any_reset = True
            continue
        if elapsed and elapsed > 0:
            metrics[f"vigor_dsl_{base}_delta{direction}"] = round(delta, 3)
            if delta > 0:
                rate = delta / elapsed * 3600.0
                metrics[f"vigor_dsl_{base}_rate_perhour{direction}"] = round(rate, 3)

    if any_reset:
        prev_uptime = state.get("last_uptime")
        if up is not None and prev_uptime is not None and up < prev_uptime - 60 and prev_uptime > 60:
            state["reboot_count"] = state.get("reboot_count", 0) + 1
        else:
            state["resync_count"] = state.get("resync_count", 0) + 1

    state["last_counter_ts"] = now
    state["last_uptime"] = up

    metrics["vigor_dsl_resync_total"] = state.get("resync_count", 0)
    metrics["vigor_dsl_reboot_total"] = state.get("reboot_count", 0)
    return metrics


# ---------------------------------------------------------------------------
# HTTP / login layer
# ---------------------------------------------------------------------------

AUTH_TOKEN_RE = re.compile(r"sFormAuthStr\s*=\s*[\"']?([A-Za-z0-9]+)[\"']?", re.I)
AUTH_ERROR_MARKERS = ("autherror", "session expired", "sformautherrstr")


class UrllibConn:
    """Cookie-aware urllib client used for real scrapes."""

    def __init__(self, timeout=8):
        self.timeout = timeout
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx),
            urllib.request.HTTPCookieProcessor(jar),
        )

    def get(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": "pymon-vigor130/1.0"})
        with self.opener.open(req, timeout=self.timeout) as resp:
            return resp.read().decode("utf-8", "replace")

    def post(self, url, fields):
        data = urllib.parse.urlencode(fields).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"User-Agent": "pymon-vigor130/1.0", "Content-Type": "application/x-www-form-urlencoded"},
        )
        with self.opener.open(req, timeout=self.timeout) as resp:
            return resp.read().decode("utf-8", "replace")


def _extract_form_action(html):
    m = re.search(r"<form[^>]*action\s*=\s*[\"']([^\"']+)[\"']", html, re.I)
    return m.group(1) if m else ""


def _extract_inputs(html):
    fields = {}
    for m in re.finditer(r"<input\b[^>]*>", html, re.I):
        tag = m.group(0)
        nm = re.search(r"name\s*=\s*[\"']([^\"']+)[\"']", tag, re.I)
        if not nm:
            continue
        vl = re.search(r"value\s*=\s*[\"']([^\"']*)[\"']", tag, re.I)
        fields[nm.group(1)] = vl.group(1) if vl else ""
    return fields


def _extract_token(html):
    m = AUTH_TOKEN_RE.search(html or "")
    return m.group(1) if m else None


def _is_autherror(html):
    low = (html or "").lower()
    return any(marker in low for marker in AUTH_ERROR_MARKERS)


def _is_dsl_page(parsed):
    """A valid scrape must expose a real line state."""
    ls = parsed.get("line_state")
    return bool(ls and str(ls).upper() not in PLACEHOLDERS)


def _url(base, path):
    if not path:
        return base
    return path if path.startswith("http") else base.rstrip("/") + path


def _try_login(conn, base, username, password):
    """Return (session_token, error_code). token is None on failure."""
    try:
        html = conn.get(_url(base, "/weblogin.htm"))
    except Exception:
        return None, "auth_error"
    action = _extract_form_action(html) or "/cgi-bin/login.cgi"
    fields = _extract_inputs(html)
    fields["sUserName"] = username
    fields["sSysPass"] = password
    fields["btnOk"] = fields.get("btnOk") or "OK"
    try:
        body = conn.post(_url(base, action), fields)
    except Exception:
        return None, "auth_error"
    token = _extract_token(body)
    if token:
        return token, None
    for path in ("/", "/index.htm", "/menu/framelist.htm", "/page/menu.htm"):
        try:
            token = _extract_token(conn.get(_url(base, path)))
        except Exception:
            continue
        if token:
            return token, None
    return None, "auth_error"


def _fetch_dsl_page(conn, base, token):
    """Return (html, error_code). Falls back to the direct doc path."""
    cgi = f"{base}/cgi-bin/V2X00.cgi?sFormAuthStr={token}&fid=2356"
    try:
        html = conn.get(cgi)
    except Exception:
        return None, "http_error"
    if _is_autherror(html) or not _is_dsl_page(parse_dsl_page(html)):
        try:
            html = conn.get(f"{base}/doc/dslstatus.sht")
        except Exception:
            return None, "http_error"
    return html, None


def _parse_duration(line: str) -> float | None:
    """Parse an uptime into seconds; None if no duration tokens found."""
    total = 0.0
    m = re.search(r"(\d+):(\d+):(\d+)(?:\.\d+)?", line)
    if m:
        total += int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    for pat, mul in (
        (r"(?i)(\d+(?:\.\d+)?)\s*(?:days?|d)\b", 86400),
        (r"(?i)(?:^|\s)(\d+(?:\.\d+)?)\s*h\b", 3600),
        (r"(?i)(?:^|\s)(\d+(?:\.\d+)?)\s*(?:min(?:ute)?s?|m)\b", 60),
        (r"(?i)(?:^|\s)(\d+(?:\.\d+)?)\s*s\b", 1),
    ):
        m = re.search(pat, line)
        if m:
            total += float(m.group(1)) * mul
    return total or None


def _fetch_uptime(conn, base, token):
    """Best-effort system uptime from the System Status page."""
    try:
        html = conn.get(f"{base}/cgi-bin/V2X00.cgi?sFormAuthStr={token}&fid=2015")
    except Exception:
        try:
            html = conn.get(f"{base}/doc/status.htm")
        except Exception:
            return None
    lines = _normalize_text(html)
    for line in lines:
        if re.search(r"(?i)up\s*time", line):
            dur = _parse_duration(line)
            if dur:
                return dur
    # value may sit in the cell following the label (td-per-cell table)
    for idx, line in enumerate(lines):
        if re.search(r"(?i)up\s*time", line) and idx + 1 < len(lines):
            dur = _parse_duration(lines[idx + 1])
            if dur:
                return dur
    return None


def run(config):
    host = (config.get("host") or "").strip()
    password = config.get("password") or ""
    if not host or not password:
        print(json.dumps({"vigor_dsl_scrape_success": 0, "vigor_dsl_error": "missing_config"}))
        return

    port = int(config.get("port", 11080))
    timeout = int(config.get("timeout", 8))
    scheme = "https" if port in (443, 11443) else "http"
    base = f"{scheme}://{host}:{port}"

    state_file = config.get("state_file") or _default_state_file()
    state = _load_state(state_file)

    start = time.time()
    conn = UrllibConn(timeout=timeout)
    token = None
    html = None
    error = None

    for attempt in range(2):
        if token is None:
            token, error = _try_login(conn, base, config.get("username", "admin"), password)
            if not token:
                break
        html, error = _fetch_dsl_page(conn, base, token)
        if html is not None and _is_dsl_page(parse_dsl_page(html)):
            break
        token = None

    if error is None and html is not None:
        parsed = parse_dsl_page(html)
        if not _is_dsl_page(parsed):
            error = "parse_error"
        else:
            uptime = _fetch_uptime(conn, base, token)
            metrics = compute_poll(state, parsed, uptime_seconds=uptime)
            metrics["vigor_dsl_scrape_duration_seconds"] = round(time.time() - start, 3)
            _save_state(state_file, state)
            print(json.dumps(metrics))
            return

    state["scrape_fail_count"] = state.get("scrape_fail_count", 0) + 1
    _save_state(state_file, state)
    print(json.dumps({"vigor_dsl_scrape_success": 0, "vigor_dsl_error": error or "unknown"}))


if __name__ == "__main__":
    try:
        cfg = json.load(sys.stdin)
    except ValueError:
        cfg = {}
    try:
        run(cfg)
    except Exception:
        print(json.dumps({"vigor_dsl_scrape_success": 0, "vigor_dsl_error": "internal"}))