"""Tests for the unifi_gateway plugin (HTTP timeout budget + error contract).

Everything is local and mocked: a fake opener for the request layer and a
throwaway loopback HTTP server for the timeout behaviour. No gateway, no
credentials and no remote access are required.

Run with the system python (the plugin imports ssl; the repo venv has a broken
_ssl):  python3 tests/test_unifi_gateway.py
"""

import http.server
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(TESTS_DIR)
PLUGIN_PATH = os.path.join(REPO_DIR, "plugins", "unifi_gateway.py")

# The agent kills a plugin after PLUGIN_TIMEOUT seconds and reports that as
# agent:error=2. The plugin must stay strictly below it.
AGENT_PLUGIN_TIMEOUT = 30

# The UCG answered with full latency right up to the old hardcoded ceiling, so
# the default timeout must be more generous than 15 s.
OLD_HARDCODED_TIMEOUT = 15


def load_plugin():
    spec = importlib.util.spec_from_file_location("unifi_gateway", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


uni = load_plugin()

RESULTS = []


def check(name, got, want):
    ok = got == want
    RESULTS.append((name, ok, got, want))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got={got!r} want={want!r}")


def check_true(name, cond, detail=""):
    check(f"{name} {detail}".strip(), bool(cond), True)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Records the timeout of every open() call, returns a canned payload."""

    def __init__(self, payload=None, exc=None):
        self.payload = {"data": []} if payload is None else payload
        self.exc = exc
        self.timeouts = []
        self.urls = []

    def open(self, req, timeout=None):
        self.timeouts.append(timeout)
        self.urls.append(req.full_url)
        if self.exc is not None:
            raise self.exc
        return FakeResponse(self.payload)


class SlowHandler(http.server.BaseHTTPRequestHandler):
    delay = 5.0

    def _respond(self):
        time.sleep(self.delay)
        body = b'{"data":[]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The client aborted on timeout — exactly what the test provokes.
            pass

    def do_GET(self):
        self._respond()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        if length:
            self.rfile.read(length)
        self._respond()

    def log_message(self, *args):
        pass


class QuietServer(http.server.HTTPServer):
    def handle_error(self, request, client_address):
        pass


def _slow_server(delay):
    SlowHandler.delay = delay
    server = QuietServer(("127.0.0.1", 0), SlowHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


# ---------------------------------------------------------------------------
# cases
# ---------------------------------------------------------------------------


def case_timeout_resolution():
    check("1 default timeout", uni._resolve_timeout(uni.DEFAULT_TIMEOUT), 25.0)
    check("1 unparsable falls back to default", uni._resolve_timeout("abc"), uni.DEFAULT_TIMEOUT)
    check("1 None falls back to default", uni._resolve_timeout(None), uni.DEFAULT_TIMEOUT)
    check("1 oversized value clamps to max", uni._resolve_timeout(9999), uni.MAX_TIMEOUT)
    check("1 undersized value clamps to min", uni._resolve_timeout(1), 5.0)
    check("1 valid value passes through", uni._resolve_timeout(10), 10.0)
    check_true("1 default exceeds the old hardcoded ceiling",
               uni.DEFAULT_TIMEOUT > OLD_HARDCODED_TIMEOUT)
    check_true("1 budget stays below the agent's plugin cap",
               uni.TOTAL_BUDGET < AGENT_PLUGIN_TIMEOUT, f"budget={uni.TOTAL_BUDGET}")


def case_slice_budget():
    far = time.monotonic() + 100
    check("2 slice returns the wanted timeout when budget is ample", uni._slice(25.0, far), 25.0)
    near = time.monotonic() + 3
    got = uni._slice(25.0, near)
    check_true("2 slice shrinks to the remaining budget", 1.0 < got <= 3.0, f"got={got:.2f}")
    try:
        uni._slice(25.0, time.monotonic() - 1)
        raised = None
    except TimeoutError:
        raised = "TimeoutError"
    check("2 exhausted budget raises", raised, "TimeoutError")


def case_requests_use_clamped_timeout():
    far = time.monotonic() + 100
    opener = FakeOpener()
    uni._api_get(opener, "http://example.invalid/x", None, 25.0, far)
    check("3 _api_get uses the per-request timeout", opener.timeouts[0], 25.0)
    opener = FakeOpener()
    uni._api_get(opener, "http://example.invalid/x", "csrf-token", 10.0, far)
    check("3 _api_get uses its own timeout", opener.timeouts[0], 10.0)

    opener = FakeOpener(payload={"data": [{"state": 1, "type": "ugw"}]})
    uni._collect(opener, "gw.invalid", "default", "csrf", 25.0, far)
    check("3 _collect polls health/device/sta", len(opener.timeouts), 3)
    check("3 each poll uses the per-request timeout", set(opener.timeouts), {25.0})
    check_true("3 polls target the proxy API path",
               all("/proxy/network/api/s/default/" in u for u in opener.urls), opener.urls)


def case_login_success_and_csrf():
    import base64

    far = time.monotonic() + 100
    token = base64.b64encode(json.dumps({"csrfToken": "CSRF"}).encode()).decode().rstrip("=")
    jar = type("J", (), {"__iter__": lambda self: iter([type("C", (), {"name": "TOKEN", "value": f"x.{token}.y"})()])})()
    opener = FakeOpener()
    err, csrf = uni._login(opener, "http://example.invalid/api/auth/login", "u", "p", jar, 25.0, far)
    check("4 login returns the csrf token", (err, csrf), (None, "CSRF"))

    opener = FakeOpener()
    empty = type("J", (), {"__iter__": lambda self: iter([])})()
    err, csrf = uni._login(opener, "http://example.invalid/api/auth/login", "u", "p", empty, 25.0, far)
    check("4 missing TOKEN cookie reported, not raised",
          (err, csrf), ("no TOKEN cookie found after login", None))


def case_rate_limit_respects_budget():
    far = time.monotonic() + 100
    err429 = urllib.error.HTTPError("http://x/", 429, "Too Many", {}, None)
    empty = type("J", (), {"__iter__": lambda self: iter([])})()
    opener = FakeOpener(exc=err429)
    start = time.monotonic()
    err, csrf = uni._login(opener, "http://example.invalid/x", "u", "p", empty, 25.0, far)
    check("5 rate limit retries then gives up", (err, csrf), ("rate limited after retries", None))
    check_true("5 four attempts were made", len(opener.timeouts) == 4, f"attempts={len(opener.timeouts)}")

    # With the budget nearly gone the retry must give up instead of sleeping.
    opener = FakeOpener(exc=err429)
    start = time.monotonic()
    err, csrf = uni._login(opener, "http://example.invalid/x", "u", "p", empty, 25.0,
                           time.monotonic() + 1.5)
    elapsed = time.monotonic() - start
    check("5 exhausted budget gives up immediately", (err, csrf), ("rate limited after retries", None))
    check_true("5 no long backoff sleep happened", elapsed < 1.0, f"elapsed={elapsed:.2f}s")

    # A non-429 error must not be swallowed.
    err500 = urllib.error.HTTPError("http://x/", 500, "Boom", {}, None)
    opener = FakeOpener(exc=err500)
    try:
        uni._login(opener, "http://example.invalid/x", "u", "p", empty, 25.0, far)
        raised = None
    except urllib.error.HTTPError as exc:
        raised = exc.code
    check("5 500 propagates to the caller", raised, 500)


def case_request_timeout_is_enforced():
    server, port = _slow_server(delay=5.0)
    try:
        opener, _jar = uni._opener(False)
        start = time.monotonic()
        try:
            uni._api_get(opener, f"http://127.0.0.1:{port}/proxy/network/api/s/default/stat/health",
                         None, 1.0, time.monotonic() + 100)
            raised = None
        except Exception as exc:  # noqa: BLE001 - any transport error is fine here
            raised = type(exc).__name__
        elapsed = time.monotonic() - start
        check_true("6 slow poll aborts near the 1 s timeout", 0.8 <= elapsed < 3.0,
                   f"elapsed={elapsed:.2f}s")
        check("6 slow poll raises a timeout", raised, "TimeoutError")
    finally:
        server.shutdown()


def case_run_budget_caps_total():
    server, port = _slow_server(delay=5.0)
    try:
        opener, _jar = uni._opener(False)
        # Generous per-request timeout, but the run budget is nearly gone: the
        # deadline must win so the run stays inside the agent's plugin cap.
        start = time.monotonic()
        try:
            uni._api_get(opener, f"http://127.0.0.1:{port}/x", None, 25.0, time.monotonic() + 1.5)
            raised = None
        except Exception as exc:  # noqa: BLE001
            raised = type(exc).__name__
        elapsed = time.monotonic() - start
        check_true("7 budget cap aborts before the per-request timeout", 1.0 <= elapsed < 3.0,
                   f"elapsed={elapsed:.2f}s")
        check("7 budget cap raises a timeout", raised, "TimeoutError")
    finally:
        server.shutdown()


def case_cli_contract():
    # Unreachable port: connection refused, so the login path fails fast.
    cfg = json.dumps({"api_host": "127.0.0.1", "username": "u", "password": "p"})
    proc = subprocess.run([sys.executable, PLUGIN_PATH], input=cfg, capture_output=True, text=True,
                          timeout=30)
    check("8 unreachable gateway exits 1", proc.returncode, 1)
    check_true("8 failure writes a concise stderr line", "login request failed" in proc.stderr)
    check_true("8 no traceback on stderr", "Traceback" not in proc.stderr)
    check_true("8 stdout carries the JSON error contract",
               json.loads(proc.stdout).get("error", "").startswith("unifi login failed"))

    proc = subprocess.run([sys.executable, PLUGIN_PATH], input="{}", capture_output=True, text=True,
                          timeout=30)
    check("9 missing api_host/password exits 1", proc.returncode, 1)
    check("9 missing api_host/password reports JSON", json.loads(proc.stdout),
          {"error": "api_host and password required"})

    proc = subprocess.run([sys.executable, PLUGIN_PATH],
                          input=json.dumps({"api_host": "127.0.0.1", "password": "p",
                                            "timeout": "not-a-number"}),
                          capture_output=True, text=True, timeout=30)
    check("10 malformed timeout does not crash the run", proc.returncode, 1)
    check_true("10 malformed timeout still reaches the login",
               "login request failed" in proc.stderr)


def case_schema():
    fields = {f["key"]: f for f in uni.__schema__["fields"]}
    check_true("11 schema exposes a timeout field", "timeout" in fields)
    check("11 timeout default", fields["timeout"]["default"], 25)
    check("11 timeout min", fields["timeout"]["min"], 5)
    check("11 timeout max", fields["timeout"]["max"], 25)
    check_true("11 timeout field is optional", fields["timeout"].get("optional") is True)


def main():
    print("unifi_gateway plugin tests")
    case_timeout_resolution()
    case_slice_budget()
    case_requests_use_clamped_timeout()
    case_login_success_and_csrf()
    case_rate_limit_respects_budget()
    case_request_timeout_is_enforced()
    case_run_budget_caps_total()
    case_cli_contract()
    case_schema()

    print()
    failed = [r for r in RESULTS if not r[1]]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} bestanden")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
