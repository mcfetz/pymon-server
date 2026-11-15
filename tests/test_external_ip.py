"""Tests for the external_ip plugin: change detection, state handling and the
first-run guard (a fresh state must not look like an IP change).

Everything is local: the provider lookup is stubbed and the state file lives in a
temporary directory. No network, no host access, stdlib only.

Run with:  python3 tests/test_external_ip.py
(Do not use venv/bin/python — that interpreter's _ssl is broken and some tests
in this suite load plugins that import ssl; this file itself needs no ssl.)
"""

import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
from contextlib import redirect_stdout

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(TESTS_DIR)
PLUGIN_PATH = os.path.join(REPO_DIR, "plugins", "external_ip.py")

TMP = tempfile.mkdtemp(prefix="pymon_external_ip_test_")

RESULTS = []


def check(name, got, want):
    ok = got == want
    RESULTS.append((name, ok, got, want))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got={got!r} want={want!r}")


def check_true(name, cond, detail=""):
    check(f"{name} {detail}".strip(), bool(cond), True)


def load_plugin():
    spec = importlib.util.spec_from_file_location("external_ip", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin = load_plugin()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def state_path(name):
    return os.path.join(TMP, name)


def write_state(path, state):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)
    return path


def read_state(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run(ip, path):
    """Execute plugin.run() with a stubbed provider. Returns the parsed metrics."""
    plugin._resolve = (
        (lambda config: ("stub", ip, 1.0)) if ip else (lambda config: (None, None, None))
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        plugin.run({"state_file": path})
    raw = buf.getvalue().strip()
    try:
        return json.loads(raw)
    except ValueError:  # pragma: no cover - would mean the plugin printed junk
        raise AssertionError(f"plugin printed non-JSON: {raw!r}")


FRESH = {"_version": 1, "last_ip": None, "change_count": 0, "last_change_ts": None}
IP = "80.128.103.211"


# ---------------------------------------------------------------------------
# 1 — first run on a fresh state (agent container replaced)
# ---------------------------------------------------------------------------


def case_fresh_state():
    path = write_state(state_path("fresh.json"), dict(FRESH))
    metrics = run(IP, path)
    check("1/1 fresh state does not report a change", metrics["external_ip_changed"], 0)
    check("1/2 fresh state does not bump change_count", metrics["external_ip_change_count"], 0)
    check_true("1/3 fresh state resolves ok", metrics.get("external_ip_ok") == 1)
    check("1/4 fresh state carries the address", metrics.get("external_ip_value"), IP)
    check_true(
        "1/5 fresh state is marked as baseline",
        read_state(path).get("baseline") is True,
        str(read_state(path)),
    )
    check("1/6 baseline stores the address", read_state(path).get("last_ip"), IP)
    # a state file that does not exist at all behaves the same (new container)
    gone = state_path("missing.json")
    if os.path.exists(gone):
        os.remove(gone)
    metrics = run(IP, gone)
    check("1/7 missing state file does not report a change", metrics["external_ip_changed"], 0)
    check_true("1/8 missing state file is created", os.path.exists(gone))


# ---------------------------------------------------------------------------
# 2 — established baseline: real changes fire, repeats stay silent
# ---------------------------------------------------------------------------


def case_established_baseline():
    path = write_state(
        state_path("base.json"),
        {"_version": 1, "baseline": True, "last_ip": "203.0.113.7", "change_count": 3,
         "last_change_ts": 1700000000.0},
    )
    before = int(time.time())
    metrics = run(IP, path)
    after = int(time.time())
    check("2/1 a real change is still reported", metrics["external_ip_changed"], 1)
    check("2/2 change_count is incremented", metrics["external_ip_change_count"], 4)
    check_true(
        "2/3 change timestamp is refreshed",
        before <= int(metrics.get("external_ip_change_ts", 0)) <= after,
        str(metrics.get("external_ip_change_ts")),
    )
    state = read_state(path)
    check("2/4 new address is persisted", state.get("last_ip"), IP)
    check_true("2/5 baseline stays set", state.get("baseline") is True)
    # second run, same address
    metrics = run(IP, path)
    check("2/6 an unchanged address stays silent", metrics["external_ip_changed"], 0)
    check("2/7 change_count does not move", metrics["external_ip_change_count"], 4)


# ---------------------------------------------------------------------------
# 3 — state written by a version without the baseline flag
# ---------------------------------------------------------------------------


def case_legacy_state():
    path = write_state(
        state_path("legacy.json"),
        {"_version": 1, "last_ip": "198.51.100.9", "change_count": 2, "last_change_ts": 1700000000.0},
    )
    metrics = run(IP, path)
    check("3/1 legacy state still reports a real change", metrics["external_ip_changed"], 1)
    check("3/2 legacy counter keeps counting", metrics["external_ip_change_count"], 3)

    path = write_state(
        state_path("legacy2.json"),
        {"_version": 1, "last_ip": IP, "change_count": 2, "last_change_ts": 1700000000.0},
    )
    metrics = run(IP, path)
    check("3/3 legacy state with the same address is silent", metrics["external_ip_changed"], 0)


# ---------------------------------------------------------------------------
# 4 — provider failures must never look like a change
# ---------------------------------------------------------------------------


def case_provider_failure():
    path = state_path("fail.json")
    if os.path.exists(path):
        os.remove(path)
    metrics = run(None, path)                                  # all providers fail
    check("4/1 provider failure reports ok=0", metrics.get("external_ip_ok"), 0)
    check_true("4/2 provider failure keeps no change flag", "external_ip_changed" not in metrics)
    check_true("4/3 provider failure sets no baseline", not os.path.exists(path))
    metrics = run(IP, path)                                    # provider recovers
    check("4/4 first success after a failure is silent", metrics["external_ip_changed"], 0)
    check("4/5 first success counts no change", metrics["external_ip_change_count"], 0)


# ---------------------------------------------------------------------------
# 5 — plugin contract
# ---------------------------------------------------------------------------


def case_contract():
    check(
        "5/1 state file lives next to the plugin (ephemeral container FS)",
        os.path.dirname(os.path.abspath(plugin._default_state_file())),
        os.path.dirname(os.path.abspath(PLUGIN_PATH)),
    )
    check(
        "5/2 state file name is unchanged",
        os.path.basename(plugin._default_state_file()),
        ".external_ip_state.json",
    )
    check(
        "5/3 fresh state default is not a baseline",
        plugin._load_state(state_path("does-not-exist.json")).get("baseline"),
        False,
    )
    fields = {f["key"] for f in plugin.__schema__["fields"]}
    check("5/4 schema still offers state_file", sorted(fields), ["sleep", "state_file", "timeout"])
    check_true(
        "5/5 the guard is documented in the module docstring",
        "baseline" in (plugin.__doc__ or "").lower()
        or "first successful poll" in (plugin.__doc__ or ""),
    )


def main():
    print("external_ip plugin tests")
    case_fresh_state()
    case_established_baseline()
    case_legacy_state()
    case_provider_failure()
    case_contract()

    print()
    failed = [r for r in RESULTS if not r[1]]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} bestanden")
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
