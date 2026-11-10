"""Tests for the synology_dsm plugin (HTTP timeout budget + error contract).

Everything is local and mocked: a fake opener for the request layer and a
throwaway loopback HTTP server for the timeout behaviour. No NAS, no
credentials and no remote access are required.

Run with:  python tests/test_synology_dsm.py
"""

import http.server
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(TESTS_DIR)
PLUGIN_PATH = os.path.join(REPO_DIR, "plugins", "synology_dsm.py")

# The agent kills a plugin after PLUGIN_TIMEOUT seconds and reports that as
# agent:error=2. The plugin must stay strictly below it.
AGENT_PLUGIN_TIMEOUT = 30


def load_plugin():
    spec = importlib.util.spec_from_file_location("synology_dsm", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


syn = load_plugin()

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
        self.payload = (
            {"success": True, "data": {"sid": "SID"}} if payload is None else payload
        )
        self.exc = exc
        self.timeouts = []

    def open(self, req, timeout=None):
        self.timeouts.append(timeout)
        if self.exc is not None:
            raise self.exc
        return FakeResponse(self.payload)


class SlowHandler(http.server.BaseHTTPRequestHandler):
    delay = 5.0

    def _respond(self):
        time.sleep(self.delay)
        body = b'{"success":true,"data":{"sid":"SID"}}'
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
    check("1 default timeout", syn._resolve_timeout(syn.DEFAULT_TIMEOUT), 25.0)
    check(
        "1 unparsable falls back to default",
        syn._resolve_timeout("abc"),
        syn.DEFAULT_TIMEOUT,
    )
    check(
        "1 None falls back to default", syn._resolve_timeout(None), syn.DEFAULT_TIMEOUT
    )
    check(
        "1 oversized value clamps to max", syn._resolve_timeout(9999), syn.MAX_TIMEOUT
    )
    check("1 undersized value clamps to min", syn._resolve_timeout(1), 5.0)
    check("1 valid value passes through", syn._resolve_timeout(10), 10.0)
    check_true("1 default exceeds the old 15 s ceiling", syn.DEFAULT_TIMEOUT > 15)
    check_true(
        "1 budget stays below the agent's plugin cap",
        syn.TOTAL_BUDGET < AGENT_PLUGIN_TIMEOUT,
        f"budget={syn.TOTAL_BUDGET}",
    )


def case_slice_budget():
    far = time.monotonic() + 100
    check(
        "2 slice returns the wanted timeout when budget is ample",
        syn._slice(25.0, far),
        25.0,
    )
    near = time.monotonic() + 3
    got = syn._slice(25.0, near)
    check_true(
        "2 slice shrinks to the remaining budget", 1.0 < got <= 3.0, f"got={got:.2f}"
    )
    try:
        syn._slice(25.0, time.monotonic() - 1)
        raised = None
    except TimeoutError:
        raised = "TimeoutError"
    check("2 exhausted budget raises", raised, "TimeoutError")


def case_requests_use_clamped_timeout():
    far = time.monotonic() + 100
    opener = FakeOpener(payload={"success": True})
    syn._get(opener, "http://example.invalid/x", 25.0, far)
    syn._post(opener, "http://example.invalid/x", "a=b", 10.0, far)
    check("3 _get uses the per-request timeout", opener.timeouts[0], 25.0)
    check("3 _post uses its own timeout", opener.timeouts[1], 10.0)


def case_login_success_and_auth_error():
    far = time.monotonic() + 100
    sid, err = syn._login(FakeOpener(), "nas", 5001, True, "u", "p", 25.0, far)
    check("4 login success returns sid", (sid, err), ("SID", None))

    denied = FakeOpener(payload={"success": False, "error": {"code": 400}})
    sid, err = syn._login(denied, "nas", 5001, True, "u", "p", 25.0, far)
    check(
        "5 login failure is reported, not raised",
        (sid, err),
        (None, "login failed (code 400)"),
    )


def case_request_timeout_is_enforced():
    server, port = _slow_server(delay=5.0)
    try:
        opener = syn._opener(False)
        start = time.monotonic()
        try:
            syn._login(
                opener, "127.0.0.1", port, False, "u", "p", 1.0, time.monotonic() + 100
            )
            raised = None
        except Exception as exc:  # noqa: BLE001 - any transport error is fine here
            raised = type(exc).__name__
        elapsed = time.monotonic() - start
        check_true(
            "6 slow login aborts near the 1 s timeout",
            elapsed < 3.0,
            f"elapsed={elapsed:.2f}s",
        )
        check_true(
            "6 slow login raises instead of hanging", raised is not None, str(raised)
        )
    finally:
        server.shutdown()


def case_run_budget_caps_total():
    server, port = _slow_server(delay=5.0)
    try:
        opener = syn._opener(False)
        # Generous per-request timeout, but the run budget is nearly gone: the
        # deadline must win so the run stays inside the agent's plugin cap.
        start = time.monotonic()
        try:
            syn._login(
                opener, "127.0.0.1", port, False, "u", "p", 25.0, time.monotonic() + 1.5
            )
            raised = None
        except Exception as exc:  # noqa: BLE001
            raised = type(exc).__name__
        elapsed = time.monotonic() - start
        check_true(
            "7 budget cap aborts before the per-request timeout",
            elapsed < 3.0,
            f"elapsed={elapsed:.2f}s",
        )
        check_true("7 budget cap raises", raised is not None, str(raised))
    finally:
        server.shutdown()


def case_cli_contract():
    # Unreachable port: connection refused, so the login path fails fast.
    cfg = json.dumps(
        {
            "host": "127.0.0.1",
            "port": 1,
            "use_https": False,
            "username": "u",
            "password": "p",
        }
    )
    proc = subprocess.run(
        [sys.executable, PLUGIN_PATH],
        input=cfg,
        capture_output=True,
        text=True,
        timeout=30,
    )
    check("8 unreachable NAS exits 1", proc.returncode, 1)
    check_true(
        "8 failure writes a concise stderr line", "login request failed" in proc.stderr
    )
    check_true("8 no traceback on stderr", "Traceback" not in proc.stderr)
    check("8 stdout stays empty on failure", proc.stdout.strip(), "")

    proc = subprocess.run(
        [sys.executable, PLUGIN_PATH],
        input="{}",
        capture_output=True,
        text=True,
        timeout=30,
    )
    check("9 missing host/password exits 1", proc.returncode, 1)
    check(
        "9 missing host/password reports JSON",
        json.loads(proc.stdout),
        {"error": "host and password required"},
    )

    proc = subprocess.run(
        [sys.executable, PLUGIN_PATH],
        input=json.dumps(
            {
                "host": "127.0.0.1",
                "port": 1,
                "use_https": False,
                "password": "p",
                "timeout": "not-a-number",
            }
        ),
        capture_output=True,
        text=True,
        timeout=30,
    )
    check("10 malformed timeout does not crash the run", proc.returncode, 1)
    check_true(
        "10 malformed timeout still reaches the login",
        "login request failed" in proc.stderr,
    )


def case_schema():
    fields = {f["key"]: f for f in syn.__schema__["fields"]}
    check_true("11 schema exposes a timeout field", "timeout" in fields)
    check("11 timeout default", fields["timeout"]["default"], 25)
    check("11 timeout min", fields["timeout"]["min"], 5)
    check("11 timeout max", fields["timeout"]["max"], 25)
    check_true(
        "11 timeout field is optional", fields["timeout"].get("optional") is True
    )


def main():
    print("synology_dsm plugin tests")
    case_timeout_resolution()
    case_slice_budget()
    case_requests_use_clamped_timeout()
    case_login_success_and_auth_error()
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
