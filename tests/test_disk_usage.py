"""Tests for the local disk_usage plugin (mountpoint skip logic).

Everything is local: /proc/mounts is a temporary file, mountpoints are temporary
directories. No host access, no privileged calls.

Run with:  venv/bin/python tests/test_disk_usage.py
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(TESTS_DIR)
PLUGIN_PATH = os.path.join(REPO_DIR, "plugins", "disk_usage.py")

TMP = tempfile.mkdtemp(prefix="pymon_disk_usage_test_")

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
    spec = importlib.util.spec_from_file_location("disk_usage", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin = load_plugin()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def mk(*names):
    """Create real directories (statvfs needs something that exists).

    The path structure is kept (below a temp root) so mountpoint regexes in the
    tests match the same way they would on a real host. Returns the real path
    for a single name, or a list of real paths.
    """
    paths = []
    for name in names:
        path = os.path.join(TMP, "mnt", name.lstrip("/") or "root")
        os.makedirs(path, exist_ok=True)
        paths.append(path)
    return paths[0] if len(paths) == 1 else paths


def key(path):
    return f"{path}:usage"


def write_mounts(rows):
    """rows: [(device, mountpoint, fstype, options)] -> path of a temp mounts file."""
    path = os.path.join(TMP, f"mounts_{len(os.listdir(TMP))}.txt")
    with open(path, "w", encoding="utf-8") as fh:
        for device, mountpoint, fstype, options in rows:
            fh.write(f"{device} {mountpoint} {fstype} {options} 0 0\n")
    return path


def esc(path):
    """Escape a path the way /proc/mounts does (backslash first, then whitespace)."""
    for char, code in (("\\", "134"), (" ", "040"), ("\t", "011"), ("\n", "012")):
        path = path.replace(char, f"\\{code}")
    return path


def run_plugin(config, mounts_path=None):
    env = dict(os.environ)
    if mounts_path is not None:
        env["PYMON_DISK_MOUNTS"] = mounts_path
    return subprocess.run(
        [sys.executable, PLUGIN_PATH],
        input=json.dumps(config),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def collect(rows, config=None):
    """Run the plugin's pure collection step over fake /proc/mounts rows."""
    config = config or {}
    skip_fstypes = plugin.compile_patterns(
        config.get("skip_fstypes", plugin.DEFAULT_SKIP_FSTYPES), fullmatch=True
    )
    excludes = plugin.compile_patterns(config.get("excludes", []), fullmatch=False)
    skip_readonly = bool(config.get("skip_readonly", True))
    entries = [(mp, fstype, opts) for _dev, mp, fstype, opts in rows]
    return plugin.collect_metrics(entries, skip_fstypes, excludes, skip_readonly)


# ---------------------------------------------------------------------------
# cases
# ---------------------------------------------------------------------------


def case_read_mounts_parsing():
    root = mk("/")
    spaced = mk("/mnt/with space")
    path = write_mounts(
        [
            ("/dev/sda1", root, "ext4", "rw,relatime"),
            ("/dev/sdb1", esc(spaced), "ext4", "rw"),
            ("tmpfs", "/run", "tmpfs", "rw,nosuid"),
        ]
    )
    entries = plugin.read_mounts(path)
    check("1/1 three entries parsed", len(entries), 3)
    check("1/2 octal escape decoded", entries[1][0], spaced)
    check("1/3 fstype and options", entries[2][1:], ("tmpfs", "rw,nosuid"))
    check_true("1/4 spaced mountpoint is a real dir", os.path.isdir(entries[1][0]))


def case_read_mounts_missing_file():
    check(
        "2/1 unreadable mounts file returns None",
        plugin.read_mounts("/nonexistent/mounts"),
        None,
    )


def case_pseudo_prefixes():
    root = mk("/")
    rows = [
        ("/dev/sda1", root, "ext4", "rw"),
        ("proc", "/proc", "proc", "rw"),
        ("sysfs", "/sys", "sysfs", "rw"),
        ("devtmpfs", "/dev", "devtmpfs", "rw"),
        ("tmpfs", "/run", "tmpfs", "rw"),
        ("tmpfs", "/var/run", "tmpfs", "rw"),
        ("tmpfs", "/run/lock", "tmpfs", "rw"),
    ]
    check("3/1 only the real root kept", list(collect(rows)), [key(root)])


def case_default_fstype_skips():
    root = mk("/")
    rows = [
        ("/dev/sda1", root, "ext4", "rw"),
        ("/dev/sr0", "/media/cdrom", "iso9660", "ro"),
        ("snapfuse", "/snap/core/1", "squashfs", "ro"),
        ("udev", "/dev/shm", "devtmpfs", "rw"),
        ("none", "/mnt/ram", "ramfs", "rw"),
        ("systemd-1", "/mnt/auto", "autofs", "rw"),
    ]
    check(
        "4/1 default skip_fstypes filters ro image fs", list(collect(rows)), [key(root)]
    )
    check_true(
        "4/2 default list",
        "squashfs" in plugin.DEFAULT_SKIP_FSTYPES
        and "iso9660" in plugin.DEFAULT_SKIP_FSTYPES,
    )
    check_true(
        "4/3 overlay is not skipped by default",
        "overlay" not in plugin.DEFAULT_SKIP_FSTYPES,
    )


def case_fstype_regex():
    root = mk("/")
    overlay = mk("/var/lib/docker/rootfs/overlayfs/abc123")
    data = mk("/mnt/data")
    rows = [
        ("/dev/sda1", root, "ext4", "rw"),
        ("overlay", overlay, "overlay", "rw"),
        ("/dev/sdc1", data, "xfs", "rw"),
    ]
    check_true("5/1 overlay kept with default list", key(overlay) in collect(rows))
    skipped = collect(rows, {"skip_fstypes": ["overlay", "xfs"]})
    check("5/2 regex list skips overlay and xfs", list(skipped), [key(root)])
    alt = collect(rows, {"skip_fstypes": ["(overlay|ext4)"]})
    check("5/3 alternation regex works", list(alt), [key(data)])


def case_skip_readonly():
    root = mk("/")
    rofs = mk("/mnt/.rofs")
    rwfs = mk("/mnt/.rwfs")
    rows = [
        ("/dev/sda1", root, "ext4", "rw"),
        ("/dev/sdb1", rofs, "ext4", "ro,relatime"),
        ("/dev/sdc1", rwfs, "ext4", "rw,relatime"),
    ]
    kept = collect(rows)
    check(
        "6/1 read-only mount skipped by default",
        sorted(kept),
        sorted([key(root), key(rwfs)]),
    )
    off = collect(rows, {"skip_readonly": False})
    check("6/2 skip_readonly=false keeps it", len(off), 3)
    # "ro" as a substring must not match: only the option list is inspected.
    roman = mk("/mnt/roman")
    fake = collect([("/dev/sdd1", roman, "ext4", "rw,relatime")])
    check(
        "7/1 'ro' inside a path does not trigger the ro skip", list(fake), [key(roman)]
    )


def case_excludes_regex():
    root = mk("/")
    rofs = mk("/mnt/.rofs")
    overlay = mk("/var/lib/docker/rootfs/overlayfs/deadbeef")
    rwfs = mk("/mnt/.rwfs")
    rows = [
        ("/dev/sda1", root, "ext4", "rw"),
        ("/dev/sdb1", rofs, "ext4", "rw"),
        ("overlay", overlay, "overlay", "rw"),
        ("/dev/sdc1", rwfs, "ext4", "rw"),
    ]
    cfg = {
        "excludes": [r"/mnt/\.rofs", r"\.rwfs", "/var/lib/docker/rootfs/overlayfs/"],
        "skip_readonly": False,
    }
    check("8/1 three regex excludes applied", list(collect(rows, cfg)), [key(root)])
    check(
        "8/2 multiple patterns are independent",
        sorted(collect(rows, {"excludes": [r"\.rwfs"], "skip_readonly": False})),
        sorted([key(root), key(rofs), key(overlay)]),
    )
    check(
        "8/3 plain substring pattern works",
        sorted(collect(rows, {"excludes": ["overlayfs"], "skip_readonly": False})),
        sorted([key(root), key(rofs), key(rwfs)]),
    )


def case_invalid_regex():
    root = mk("/")
    rofs = mk("/mnt/.rofs")
    rows = [
        ("/dev/sda1", root, "ext4", "rw"),
        ("/dev/sdb1", rofs, "ext4", "rw"),
    ]
    metrics = collect(
        rows, {"excludes": ["[unclosed", r"/mnt/\.rofs"], "skip_readonly": False}
    )
    check(
        "9/1 invalid regex ignored, valid one still applies", list(metrics), [key(root)]
    )
    proc = run_plugin({"excludes": ["*bad"], "skip_readonly": False})
    check("9/2 invalid regex does not break the plugin", proc.returncode, 0)
    check_true("9/3 warning on stderr", "invalid regex" in proc.stderr)


def case_plugin_contract():
    root = mk("/")
    rofs = mk("/mnt/.rofs")
    overlay = mk("/var/lib/docker/rootfs/overlayfs/abc")
    mounts = write_mounts(
        [
            ("/dev/sda1", root, "ext4", "rw,relatime"),
            ("/dev/sdb1", rofs, "ext4", "ro,relatime"),
            ("overlay", overlay, "overlay", "rw,relatime"),
            ("proc", "/proc", "proc", "rw"),
        ]
    )
    proc = run_plugin(
        {
            "sleep": 60,
            "excludes": [r"/mnt/\.rofs", "/var/lib/docker/rootfs/overlayfs/"],
        },
        mounts_path=mounts,
    )
    check("10/1 exit code 0", proc.returncode, 0)
    payload = json.loads(proc.stdout)
    check("10/2 no noise mountpoints in output", list(payload), [key(root)])
    check_true("10/3 value is numeric", isinstance(payload[key(root)], (int, float)))
    check_true(
        "10/4 metric name ends with :usage", all(k.endswith(":usage") for k in payload)
    )
    check_true("10/5 value within 0-100", 0.0 <= payload[key(root)] <= 100.0)

    missing = run_plugin({}, mounts_path="/nonexistent/mounts")
    check("10/6 unreadable mounts file exits 1", missing.returncode, 1)
    check_true("10/7 error reported on stderr", "cannot read" in missing.stderr)
    check("10/8 no stdout on failure", missing.stdout, "")


def case_schema_contract():
    fields = {f["key"]: f for f in plugin.__schema__["fields"]}
    check(
        "11/1 schema keys",
        sorted(fields),
        ["excludes", "skip_fstypes", "skip_readonly", "sleep"],
    )
    check(
        "11/2 excludes is an array of strings",
        fields["excludes"]["type"],
        "array:string",
    )
    check(
        "11/3 skip_fstypes is an array of strings",
        fields["skip_fstypes"]["type"],
        "array:string",
    )
    check("11/4 skip_readonly is a boolean", fields["skip_readonly"]["type"], "boolean")
    check(
        "11/5 skip_readonly defaults to true", fields["skip_readonly"]["default"], True
    )
    check(
        "11/6 schema default matches code default",
        fields["skip_fstypes"]["default"],
        plugin.DEFAULT_SKIP_FSTYPES,
    )
    check("11/7 sleep kept for compatibility", fields["sleep"]["default"], 60)


def main():
    print("disk_usage plugin tests")
    case_read_mounts_parsing()
    case_read_mounts_missing_file()
    case_pseudo_prefixes()
    case_default_fstype_skips()
    case_fstype_regex()
    case_skip_readonly()
    case_excludes_regex()
    case_invalid_regex()
    case_plugin_contract()
    case_schema_contract()

    print()
    failed = [r for r in RESULTS if not r[1]]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} bestanden")
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
