"""Tests for the local ucg_eth4 plugin (UCG eth4 sysfs + ppp0 PPPoE probe).

Everything is mocked: sysfs is a temporary directory tree and the single
`ip` call is a fake runner. No UCG, no remote host and no privileged access
is required.

Run with:  python tests/test_ucg_eth4.py
"""
import ast
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(TESTS_DIR)
PLUGIN_PATH = os.path.join(REPO_DIR, "plugins", "ucg_eth4.py")
AGENT_PLUGIN_PATH = os.path.join(
    os.path.dirname(REPO_DIR), "pymon-agent", "plugins", "ucg_eth4.py"
)

TMP = tempfile.mkdtemp(prefix="pymon_ucg_test_")

METRIC_PREFIXES = ("ucg_eth4_", "ucg_pppoe_", "ucg_probe_")
REQUIRED_KEYS = (
    "ucg_probe_success",
    "ucg_probe_error",
    "ucg_eth4_carrier",
    "ucg_eth4_carrier_changes_total",
    "ucg_eth4_carrier_changes_delta",
    "ucg_eth4_rx_errors_total",
    "ucg_eth4_rx_errors_delta",
    "ucg_eth4_rx_crc_errors_total",
    "ucg_eth4_rx_crc_errors_delta",
    "ucg_pppoe_up",
    "ucg_pppoe_ip",
    "ucg_pppoe_query_success",
    "ucg_pppoe_query_error",
    "ucg_probe_baseline",
    "ucg_probe_counter_reset",
)

# Anything that would mean remote access, credentials or a state change.
FORBIDDEN_CODE_WORDS = (
    "ssh",
    "scp",
    "sftp",
    "paramiko",
    "sshpass",
    "asyncssh",
    "fabric",
    "pexpect",
    "telnet",
    "curl",
    "wget",
    "urllib",
    "requests",
    "socket",
    "unifi",
    "password",
    "passwd",
    "token",
    "secret",
    "api_key",
    "apikey",
    "iptables",
    "nft",
    "systemctl",
    "ifconfig",
    "reboot",
    "shutdown",
    "addr add",
    "addr del",
    "ip link set",
)

STATE_ALLOWED_KEYS = {"_version", "interface", "pppoe_interface", "baseline", "last_success_at", "counters"}


def load_plugin():
    spec = importlib.util.spec_from_file_location("ucg_eth4", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ucg = load_plugin()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _sysfs(tmp, iface="eth4", carrier="1", carrier_changes="49", rx_errors="0",
           rx_crc_errors="0", operstate="up", speed="1000", duplex="full",
           skip=(), ppp=True):
    """Create a fake /sys/class/net tree and return its root path."""
    root = Path(tmp) / "class" / "net"
    base = root / iface
    (base / "statistics").mkdir(parents=True, exist_ok=True)

    for name, content in (
        ("carrier", carrier),
        ("carrier_changes", carrier_changes),
        ("operstate", operstate),
        ("speed", speed),
        ("duplex", duplex),
    ):
        if name in skip:
            continue
        (base / name).write_text(content)

    for name, content in (("rx_errors", rx_errors), ("rx_crc_errors", rx_crc_errors)):
        if name in skip:
            continue
        (base / "statistics" / name).write_text(content)

    if ppp:
        (root / "ppp0").mkdir(parents=True, exist_ok=True)
    return str(root)


class FakeIp:
    """Stand-in for the read-only `ip -4 -o addr show` invocation."""

    def __init__(self, stdout="", returncode=0, exc=None):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode
        self.exc = exc
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        if self.exc is not None:
            raise self.exc
        return self


def _cfg(tmp, **overrides):
    cfg = {"sleep": 30, "interface": "eth4", "pppoe_interface": "ppp0",
           "state_file": os.path.join(tmp, "state.json")}
    cfg.update(overrides)
    return cfg


def _poll(cfg, sysfs_root, runner=None):
    """One probe plus the same state persistence the plugin's run() does."""
    metrics, state, state_path = ucg.probe(cfg, sysfs_root=sysfs_root, runner=runner)
    if metrics.get("ucg_probe_success") == 1 and state_path:
        ucg._save_state(state_path, state)
    return metrics


def _code_only(path):
    """Source text with comments and docstrings blanked out."""
    src = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(src)
    drop = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            drop.update(range(body[0].lineno, body[0].end_lineno + 1))
    return "\n".join("" if i in drop else line for i, line in enumerate(src.splitlines(), 1))


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
# cases
# ---------------------------------------------------------------------------


def case_full_snapshot():
    tmp = tempfile.mkdtemp(dir=TMP)
    root = _sysfs(tmp, carrier="1", carrier_changes="49", rx_errors="3", rx_crc_errors="7")
    ip = FakeIp(stdout="4: ppp0    inet 80.128.111.55 peer 80.128.111.1 scope global ppp0\\       valid_lft forever\n")
    m = _poll(_cfg(tmp), root, ip)
    check("1 full snapshot: probe ok", m.get("ucg_probe_success"), 1)
    check("1 full snapshot: error none", m.get("ucg_probe_error"), "none")
    check("1 full snapshot: carrier", m.get("ucg_eth4_carrier"), 1)
    check("1 full snapshot: carrier_changes_total", m.get("ucg_eth4_carrier_changes_total"), 49)
    check("1 full snapshot: carrier_changes_delta", m.get("ucg_eth4_carrier_changes_delta"), 0)
    check("1 full snapshot: rx_errors_total", m.get("ucg_eth4_rx_errors_total"), 3)
    check("1 full snapshot: rx_crc_errors_total", m.get("ucg_eth4_rx_crc_errors_total"), 7)
    check("1 full snapshot: pppoe_up", m.get("ucg_pppoe_up"), 1)
    check("1 full snapshot: pppoe_ip is bare ipv4", m.get("ucg_pppoe_ip"), "80.128.111.55")
    check("1 full snapshot: operstate", m.get("ucg_eth4_operstate"), "up")
    check("1 full snapshot: speed", m.get("ucg_eth4_speed_mbps"), 1000)
    check("1 full snapshot: full duplex", m.get("ucg_eth4_full_duplex"), 1)
    check("1 full snapshot: ip argv is read-only", ip.calls[0][0],
          ["ip", "-4", "-o", "addr", "show", "dev", "ppp0", "scope", "global"])
    check("1 full snapshot: ip argv has a timeout", ip.calls[0][1] > 0, True)


def case_carrier_up_down():
    tmp = tempfile.mkdtemp(dir=TMP)
    root = _sysfs(tmp, carrier="1")
    m = _poll(_cfg(tmp), root, FakeIp(stdout="4: ppp0 inet 80.128.111.55/23 scope global ppp0\n"))
    check("2 carrier=1", m.get("ucg_eth4_carrier"), 1)

    tmp2 = tempfile.mkdtemp(dir=TMP)
    root2 = _sysfs(tmp2, carrier="0", operstate="down", duplex="")
    m2 = _poll(_cfg(tmp2), root2, FakeIp())
    check("3 carrier=0", m2.get("ucg_eth4_carrier"), 0)
    check("3 carrier=0 still a successful probe", m2.get("ucg_probe_success"), 1)
    check("3 operstate reported", m2.get("ucg_eth4_operstate"), "down")
    check_true("3 unknown duplex omitted, not 0", "ucg_eth4_full_duplex" not in m2)


def case_carrier_change_delta():
    tmp = tempfile.mkdtemp(dir=TMP)
    root = _sysfs(tmp, carrier="1", carrier_changes="49")
    cfg = _cfg(tmp)
    _poll(cfg, root, FakeIp())
    m = _poll(cfg, root, FakeIp())
    check("4 baseline poll has no reset", m.get("ucg_probe_counter_reset"), 0)

    Path(root, "eth4", "carrier_changes").write_text("50")
    m = _poll(cfg, root, FakeIp())
    check("4 carrier change delta 1", m.get("ucg_eth4_carrier_changes_delta"), 1)
    check("4 carrier change total", m.get("ucg_eth4_carrier_changes_total"), 50)
    check("4 baseline flag cleared", m.get("ucg_probe_baseline"), 0)

    Path(root, "eth4", "carrier_changes").write_text("52")
    m = _poll(cfg, root, FakeIp())
    check("5 carrier change delta >1", m.get("ucg_eth4_carrier_changes_delta"), 2)
    check("5 link back up is still visible via delta", m.get("ucg_eth4_carrier"), 1)


def case_error_deltas():
    tmp = tempfile.mkdtemp(dir=TMP)
    root = _sysfs(tmp, rx_errors="10", rx_crc_errors="20")
    cfg = _cfg(tmp)
    _poll(cfg, root, FakeIp())
    Path(root, "eth4", "statistics", "rx_errors").write_text("15")
    Path(root, "eth4", "statistics", "rx_crc_errors").write_text("21")
    m = _poll(cfg, root, FakeIp())
    check("7 rx error delta", m.get("ucg_eth4_rx_errors_delta"), 5)
    check("7 rx error total", m.get("ucg_eth4_rx_errors_total"), 15)
    check("6 rx crc delta", m.get("ucg_eth4_rx_crc_errors_delta"), 1)
    check("6 rx crc total", m.get("ucg_eth4_rx_crc_errors_total"), 21)
    check("6 no reset flagged", m.get("ucg_probe_counter_reset"), 0)


def case_pppoe_states():
    tmp = tempfile.mkdtemp(dir=TMP)
    root = _sysfs(tmp)
    up = _poll(_cfg(tmp), root, FakeIp(stdout="4: ppp0    inet 80.128.111.55/23 scope global ppp0\\       valid_lft forever\n"))
    check("8 pppoe up", (up.get("ucg_pppoe_up"), up.get("ucg_pppoe_ip")), (1, "80.128.111.55"))
    check("8 pppoe query ok", (up.get("ucg_pppoe_query_success"), up.get("ucg_pppoe_query_error")), (1, "none"))

    down = _poll(_cfg(tmp), root, FakeIp(stdout=""))
    check("9 pppoe down without global ipv4", (down.get("ucg_pppoe_up"), down.get("ucg_pppoe_ip")), (0, ""))
    check_true("9 pppoe down is a valid measurement",
               down.get("ucg_probe_success") == 1 and down.get("ucg_pppoe_query_success") == 1)

    scope_local = _poll(_cfg(tmp), root, FakeIp(stdout="4: ppp0    inet 10.64.0.5 peer 10.64.0.1 scope link ppp0\\       valid_lft forever\n"))
    check_true("9 non-global scope counts as down", scope_local.get("ucg_pppoe_up") == 0)

    tmp2 = tempfile.mkdtemp(dir=TMP)
    root2 = _sysfs(tmp2, ppp=False)
    missing = _poll(_cfg(tmp2), root2, FakeIp())
    check("10 missing ppp0 -> pppoe down", (missing.get("ucg_pppoe_up"), missing.get("ucg_pppoe_ip")), (0, ""))
    check("10 missing ppp0 -> interface_down", missing.get("ucg_pppoe_query_error"), "interface_down")
    check_true("10 missing ppp0 keeps the ethernet probe valid", missing.get("ucg_probe_success") == 1)


def case_baseline_and_reset():
    tmp = tempfile.mkdtemp(dir=TMP)
    root = _sysfs(tmp, carrier_changes="100", rx_errors="50", rx_crc_errors="50")
    cfg = _cfg(tmp)
    first = _poll(cfg, root, FakeIp())
    check("11 first poll flags baseline", first.get("ucg_probe_baseline"), 1)
    check("11 first poll has no reset", first.get("ucg_probe_counter_reset"), 0)
    check_true("11 first poll deltas are all zero",
               all(first[k] == 0 for k in ("ucg_eth4_carrier_changes_delta",
                                           "ucg_eth4_rx_errors_delta",
                                           "ucg_eth4_rx_crc_errors_delta")))

    second = _poll(cfg, root, FakeIp())
    check("11 second poll clears baseline", second.get("ucg_probe_baseline"), 0)

    for path, value in (("eth4/carrier_changes", "0"), ("eth4/statistics/rx_errors", "2"), ("eth4/statistics/rx_crc_errors", "1")):
        Path(root, path).write_text(value)
    m = _poll(cfg, root, FakeIp())
    check("12 counter reset flagged", m.get("ucg_probe_counter_reset"), 1)
    check("12 reset deltas are 0", (m.get("ucg_eth4_carrier_changes_delta"), m.get("ucg_eth4_rx_errors_delta"),
                                    m.get("ucg_eth4_rx_crc_errors_delta")), (0, 0, 0))
    check("12 reset values become the new baseline", (m.get("ucg_eth4_carrier_changes_total"),
                                                       m.get("ucg_eth4_rx_errors_total")), (0, 2))

    Path(root, "eth4/statistics/rx_crc_errors").write_text("4")
    after = _poll(cfg, root, FakeIp())
    check("12 delta resumes from the new baseline", after.get("ucg_eth4_rx_crc_errors_delta"), 3)
    check("12 reset flag cleared again", after.get("ucg_probe_counter_reset"), 0)


def case_ethernet_failures():
    tmp = tempfile.mkdtemp(dir=TMP)
    root = os.path.join(tmp, "class", "net")
    os.makedirs(root)
    (Path(root) / "ppp0").mkdir()
    missing = _poll(_cfg(tmp), root, FakeIp())
    check("13 missing eth4 -> probe_success 0", missing.get("ucg_probe_success"), 0)
    check("13 missing eth4 -> missing_interface", missing.get("ucg_probe_error"), "missing_interface")
    check_true("13 no stale carrier emitted", "ucg_eth4_carrier" not in missing)
    check("13 pppoe still measured without eth4", missing.get("ucg_pppoe_up"), 0)
    check_true("13 missing eth4 does not overwrite state", not Path(tmp, "state.json").exists())

    for relpath, expected in (("eth4/carrier_changes", "missing_counter"),
                              ("eth4/statistics/rx_errors", "missing_counter"),
                              ("eth4/statistics/rx_crc_errors", "missing_counter")):
        tmp2 = tempfile.mkdtemp(dir=TMP)
        root2 = _sysfs(tmp2, skip=(Path(relpath).name,))
        m = _poll(_cfg(tmp2), root2, FakeIp())
        check(f"14 missing primary counter {relpath}", (m.get("ucg_probe_success"), m.get("ucg_probe_error")), (0, expected))

    for bad in ("not-a-number", "-3", ""):
        tmp3 = tempfile.mkdtemp(dir=TMP)
        root3 = _sysfs(tmp3)
        Path(root3, "eth4", "carrier_changes").write_text(bad)
        m = _poll(_cfg(tmp3), root3, FakeIp())
        check(f"15 invalid sysfs value {bad!r}", (m.get("ucg_probe_success"), m.get("ucg_probe_error")), (0, "parse_error"))

    tmp4 = tempfile.mkdtemp(dir=TMP)
    root4 = _sysfs(tmp4, carrier="2")
    m = _poll(_cfg(tmp4), root4, FakeIp())
    check("15 carrier outside {0,1} is a parse_error", m.get("ucg_probe_error"), "parse_error")

    for bad in ("../../etc/passwd", "eth4/../eth5", "eth 4", "", "a" * 16, ".hidden", "eth;rm"):
        m = _poll(_cfg(tmp4, interface=bad), root4, FakeIp())
        check(f"15 unsafe interface {bad!r} rejected", (m.get("ucg_probe_success"), m.get("ucg_probe_error")), (0, "invalid_config"))


def case_pppoe_query_failures():
    tmp = tempfile.mkdtemp(dir=TMP)
    root = _sysfs(tmp)
    cfg = _cfg(tmp)

    missing_bin = _poll(cfg, root, FakeIp(exc=FileNotFoundError(2, "No such file or directory")))
    check("16 missing ip binary", (missing_bin.get("ucg_pppoe_query_success"), missing_bin.get("ucg_pppoe_query_error")), (0, "command_missing"))
    check_true("16 ethernet probe unaffected by ip failure", missing_bin.get("ucg_probe_success") == 1)

    timeout = _poll(cfg, root, FakeIp(exc=subprocess.TimeoutExpired(cmd="ip", timeout=5)))
    check("17 pppoe query timeout", (timeout.get("ucg_pppoe_query_success"), timeout.get("ucg_pppoe_query_error")), (0, "command_timeout"))
    check_true("17 timeout is not a missing probe", timeout.get("ucg_probe_success") == 1)

    failed = _poll(cfg, root, FakeIp(stdout="", returncode=1, ))
    check("17 non-zero exit -> command_failed", (failed.get("ucg_pppoe_query_success"), failed.get("ucg_pppoe_query_error")), (0, "command_failed"))

    garbage = _poll(cfg, root, FakeIp(stdout="gibberish without inet\n"))
    check("17 unparseable output -> parse_error", (garbage.get("ucg_pppoe_query_success"), garbage.get("ucg_pppoe_query_error")), (0, "parse_error"))

    ipv6 = _poll(cfg, root, FakeIp(stdout="4: ppp0    inet 2001:db8::1/64 scope global ppp0\n"))
    check_true("17 ipv6 never becomes pppoe_ip", ipv6.get("ucg_pppoe_up") == 0 and ipv6.get("ucg_pppoe_ip") == "")


def case_error_does_not_overwrite_state():
    tmp = tempfile.mkdtemp(dir=TMP)
    root = _sysfs(tmp, carrier="1", carrier_changes="12", rx_errors="3", rx_crc_errors="4")
    cfg = _cfg(tmp)
    good = _poll(cfg, root, FakeIp())
    state_path = Path(tmp, "state.json")
    before = state_path.read_text()
    check_true("18 good poll wrote the state file", "carrier_changes" in json.loads(before)["counters"])

    shutil.rmtree(Path(root, "eth4"))
    bad = _poll(cfg, root, FakeIp())
    check("18 failing poll reports the error", (bad.get("ucg_probe_success"), bad.get("ucg_probe_error")), (0, "missing_interface"))
    check("18 state file untouched by a failing poll", state_path.read_text(), before)
    check_true("18 no stale totals emitted", "ucg_eth4_carrier_changes_total" not in bad)

    # Recovery resumes from the untouched baseline.
    _sysfs(tmp, carrier="1", carrier_changes="13", rx_errors="3", rx_crc_errors="4")
    back = _poll(cfg, root, FakeIp())
    check("18 recovery uses the preserved baseline", back.get("ucg_eth4_carrier_changes_delta"), 1)
    check("18 counters are now current again", back.get("ucg_probe_success"), 1)


def case_no_raw_errors_or_secrets():
    tmp = tempfile.mkdtemp(dir=TMP)
    root = _sysfs(tmp, ppp=False)
    failures = {
        "missing_interface": _poll(_cfg(tempfile.mkdtemp(dir=TMP)), os.path.join(tempfile.mkdtemp(dir=TMP), "class", "net"), FakeIp()),
        "read_error": _read_error_probe(),
        "parse_error": _poll(_cfg(tempfile.mkdtemp(dir=TMP)), _sysfs(tempfile.mkdtemp(dir=TMP), carrier="x"), FakeIp()),
        "invalid_config": _poll(_cfg(tempfile.mkdtemp(dir=TMP), interface="../eth4"), root, FakeIp()),
    }
    ip_faults = [
        FakeIp(exc=FileNotFoundError(2, "/usr/sbin/ip: No such file or directory")),
        FakeIp(exc=subprocess.TimeoutExpired(cmd="ip", timeout=5)),
        FakeIp(stdout="/usr/sbin/ip: segmentation fault at 0x0 (core dumped)", returncode=139),
        FakeIp(stdout="Device \"ppp0\" does not exist.\n", returncode=1),
    ]
    for fault in ip_faults:
        failures["pppoe:" + str(fault.returncode)] = _poll(_cfg(tempfile.mkdtemp(dir=TMP)), _sysfs(tempfile.mkdtemp(dir=TMP)), fault)

    for name, m in failures.items():
        check(f"19 {name}: eth error in taxonomy", m.get("ucg_probe_error") in ucg.ETH_ERRORS, True)
        check(f"19 {name}: pppoe error in taxonomy", m.get("ucg_pppoe_query_error") in ucg.PPPOE_ERRORS, True)
        blob = json.dumps(m)
        check_true(f"19 {name}: no raw message/path/command in metrics",
                   "Traceback" not in blob and "/usr" not in blob and "ip -4" not in blob
                   and "core dumped" not in blob and "\\n" not in blob)

    # 20: state file and fixtures hold technical data only.
    tmp2 = tempfile.mkdtemp(dir=TMP)
    root2 = _sysfs(tmp2, carrier="1", carrier_changes="5")
    _poll(_cfg(tmp2), root2, FakeIp(stdout="4: ppp0 inet 80.128.111.55/23 scope global ppp0\n"))
    state_text = Path(tmp2, "state.json").read_text()
    state = json.loads(state_text)
    check("20 state keys are technical only", set(state) <= STATE_ALLOWED_KEYS, True)
    check_true("20 state holds no address, no command, no credential",
               "80.128" not in state_text and "ip -4" not in state_text
               and not any(w in state_text.lower() for w in ("password", "token", "secret", "ssh")))
    check_true("20 no temp state file left behind", not Path(f"{tmp2}/state.json.tmp").exists())

    schema_fields = [f["key"] for f in ucg.__schema__["fields"]]
    check("20 schema has no credential field",
          [f for f in schema_fields if f in ("password", "username", "api_key", "token", "ssh_key", "host", "api_host")], [])


def _read_error_probe():
    """An unreadable sysfs file yields read_error, not an exception."""
    tmp = tempfile.mkdtemp(dir=TMP)
    root = _sysfs(tmp)
    target = Path(root, "eth4", "carrier_changes")
    target.unlink()
    os.symlink("/nonexistent/pymon_ucg_eth4", target)
    return _poll(_cfg(tmp), root, FakeIp())


def case_no_ssh_and_no_writes():
    for path in (PLUGIN_PATH, AGENT_PLUGIN_PATH):
        code = _code_only(path)
        lowered = code.lower()
        found = [w for w in FORBIDDEN_CODE_WORDS if w in lowered]
        check(f"21/22 no remote/credential/write keywords in {os.path.basename(os.path.dirname(os.path.dirname(path)))}/{os.path.basename(path)}",
              found, [])
        check_true(f"22 no shell=True in {os.path.basename(path)}", "shell=True" not in code and "shell = True" not in code)

    tree = ast.parse(Path(PLUGIN_PATH).read_text(encoding="utf-8"))
    imported = set()
    subprocess_calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess":
            subprocess_calls.append(node)
    check("21 no ssh/paramiko-style import", sorted(imported & {"paramiko", "ssh", "asyncssh", "pexpect", "fabric", "telnetlib"}), [])
    check_true("22 subprocess is used at all (else nothing to audit)", len(subprocess_calls) >= 1)

    allowed = {"ip", "-4", "-o", "addr", "show", "dev", "scope", "global"}
    assigned_lists = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, (ast.List, ast.Tuple))):
            continue
        elts = node.value.elts
        # argv may interpolate the validated interface name, so Name elements
        # are allowed; only the literal arguments are audited.
        if not elts or not all(isinstance(e, ast.Constant) and isinstance(e.value, str) or isinstance(e, ast.Name)
                               for e in elts):
            continue
        items = [e.value for e in elts if isinstance(e, ast.Constant)]
        for target in node.targets:
            if isinstance(target, ast.Name):
                assigned_lists[target.id] = items

    audited = 0
    for call in subprocess_calls:
        check_true("22 subprocess call passes an argument list, not a command string",
                   isinstance(call.args[0], (ast.Name, ast.List, ast.Tuple)))
        argv = None
        if isinstance(call.args[0], ast.Name):
            argv = assigned_lists.get(call.args[0].id)
        elif isinstance(call.args[0], (ast.List, ast.Tuple)):
            argv = [e.value for e in call.args[0].elts if isinstance(e, ast.Constant)]
        check_true("22 argv resolves to a literal list", isinstance(argv, list))
        if argv:
            audited += 1
            check(f"22 argv {argv} contains only read-only ip arguments", sorted(set(argv) - allowed), [])
        check_true("22 shell is not enabled",
                   not any(k.arg == "shell" and getattr(k.value, "value", None) for k in call.keywords))
        check_true("22 subprocess call has a timeout",
                   any(k.arg == "timeout" for k in call.keywords))
    check_true("22 at least one argv was audited", audited >= 1)


def case_schema_and_json_contract():
    fields = {f["key"]: f for f in ucg.__schema__["fields"]}
    check("23 sleep default is 30s", fields["sleep"]["default"], 30)
    check("23 sleep minimum is 30s", fields["sleep"]["min"], 30)
    check("23 interface default is eth4", fields["interface"]["default"], "eth4")
    check("23 pppoe_interface default is ppp0", fields["pppoe_interface"]["default"], "ppp0")

    tmp = tempfile.mkdtemp(dir=TMP)
    root = _sysfs(tmp, carrier="1", carrier_changes="4", rx_errors="0", rx_crc_errors="0")
    state_file = os.path.join(tmp, "state.json")
    env = dict(os.environ, PYMON_UCG_SYSFS_ROOT=root)
    proc = subprocess.run([sys.executable, PLUGIN_PATH], input=json.dumps(_cfg(tmp)),
                          capture_output=True, text=True, timeout=30, env=env)
    check("24 agent contract: exit code 0", proc.returncode, 0)
    check("24 agent contract: stderr empty", proc.stderr.strip(), "")
    try:
        payload = json.loads(proc.stdout)
        parsed = True
    except ValueError:
        payload, parsed = {}, False
    check_true("24 stdout is a single JSON object", parsed and isinstance(payload, dict))
    check_true("24 keys are strings, values are int/float/str",
               all(isinstance(k, str) and isinstance(v, (int, float, str)) and not isinstance(v, bool)
                   for k, v in payload.items()))
    check_true("24 required keys present", set(REQUIRED_KEYS) <= set(payload))
    check_true("24 all metric names are prefixed",
               all(k.startswith(METRIC_PREFIXES) for k in payload))
    check("24 mock run reports carrier", payload.get("ucg_eth4_carrier"), 1)
    check_true("24 no NaN/Infinity in output",
               "NaN" not in proc.stdout and "Infinity" not in proc.stdout)

    proc2 = subprocess.run([sys.executable, PLUGIN_PATH], input="{}", capture_output=True,
                           text=True, timeout=30, env=env)
    check("24 empty config still yields valid JSON", json.loads(proc2.stdout).get("ucg_probe_success"), 1)

    proc3 = subprocess.run([sys.executable, PLUGIN_PATH], input="not json",
                           capture_output=True, text=True, timeout=30, env=env)
    check("24 malformed stdin does not crash the agent",
          (proc3.returncode, json.loads(proc3.stdout).get("ucg_probe_success")), (0, 1))

    # 25: an expected ethernet probe error is reported as probe_success=0.
    empty = tempfile.mkdtemp(dir=TMP)
    empty_root = os.path.join(empty, "class", "net")
    os.makedirs(empty_root)
    proc4 = subprocess.run([sys.executable, PLUGIN_PATH], input=json.dumps(_cfg(empty)),
                           capture_output=True, text=True, timeout=30,
                           env=dict(os.environ, PYMON_UCG_SYSFS_ROOT=empty_root))
    payload4 = json.loads(proc4.stdout)
    check("25 missing eth4 -> probe_success 0", payload4.get("ucg_probe_success"), 0)
    check("25 missing eth4 -> error class", payload4.get("ucg_probe_error"), "missing_interface")
    check("25 state file still written path untouched", os.path.exists(os.path.join(empty, "state.json")), False)
    check("25 agent contract preserved on error payload", proc4.returncode, 0)

    check_true("24/25 agent copy is byte-identical to the server copy",
               Path(AGENT_PLUGIN_PATH).read_bytes() == Path(PLUGIN_PATH).read_bytes())


def main():
    print("ucg_eth4 plugin tests")
    case_full_snapshot()
    case_carrier_up_down()
    case_carrier_change_delta()
    case_error_deltas()
    case_pppoe_states()
    case_baseline_and_reset()
    case_ethernet_failures()
    case_pppoe_query_failures()
    case_error_does_not_overwrite_state()
    case_no_raw_errors_or_secrets()
    case_no_ssh_and_no_writes()
    case_schema_and_json_contract()

    print()
    failed = [r for r in RESULTS if not r[1]]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} bestanden")
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())