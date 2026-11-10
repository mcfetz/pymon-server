#!/usr/bin/env python3
"""disk_usage.py - Disk usage per mountpoint via os.statvfs. No external deps.

Metrics (one per mountpoint):
  ``<mountpoint>:usage`` - used space in percent (0-100).

Skips, in this order:
  1. pseudo filesystems whose mountpoint starts with /sys, /proc, /dev,
     /run or /var/run;
  2. filesystem types matching ``skip_fstypes`` (regex, full match) - default
     are read-only image/device filesystems that can never be relieved;
  3. read-only mounts when ``skip_readonly`` is true (a read-only mount can
     never be relieved, so an alarm on it can never clear on its own);
  4. mountpoints matching any regex in ``excludes`` (regex, searched anywhere
     in the path).

Config (stdin JSON): sleep, skip_fstypes, skip_readonly, excludes.
"""

import json
import os
import re
import sys

# Read-only image/device filesystems: never contain data the operator can free.
DEFAULT_SKIP_FSTYPES = ["squashfs", "devtmpfs", "ramfs", "autofs", "iso9660"]

# Mountpoints below these prefixes are pseudo filesystems, never disk pressure.
SKIP_MOUNT_PREFIXES = ("/sys", "/proc", "/dev", "/run", "/var/run")

__schema__ = {
    "label": "Disk",
    "description": "Disk usage per mountpoint",
    "fields": [
        {
            "key": "sleep",
            "label": "Interval (s)",
            "type": "number",
            "default": 60,
            "min": 5,
        },
        {
            "key": "excludes",
            "label": "Excluded mountpoints (regex)",
            "type": "array:string",
            "default": [],
            "optional": True,
        },
        {
            "key": "skip_fstypes",
            "label": "Skipped filesystem types (regex)",
            "type": "array:string",
            # Literal copy of DEFAULT_SKIP_FSTYPES: the server reads __schema__
            # with ast.literal_eval and cannot evaluate a call. tests/test_disk_usage.py
            # keeps both in sync.
            "default": ["squashfs", "devtmpfs", "ramfs", "autofs", "iso9660"],
            "optional": True,
        },
        {
            "key": "skip_readonly",
            "label": "Skip read-only mounts",
            "type": "boolean",
            "default": True,
            "optional": True,
        },
    ],
}


def compile_patterns(patterns, *, fullmatch):
    """Compile a list of regex strings, ignoring (and reporting) invalid ones."""
    compiled = []
    for pattern in patterns or []:
        if not isinstance(pattern, str) or not pattern:
            continue
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            print(
                f"disk_usage: ignoring invalid regex {pattern!r}: {exc}",
                file=sys.stderr,
            )
    return compiled


def matches(compiled, value, *, fullmatch):
    if fullmatch:
        return any(p.fullmatch(value) is not None for p in compiled)
    return any(p.search(value) is not None for p in compiled)


def unescape_mountpoint(path):
    """Decode the octal escapes /proc/mounts uses for space, tab, newline, backslash."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), path)


def read_mounts(path="/proc/mounts"):
    """Return [(mountpoint, fstype, options)] from a /proc/mounts style file.

    Returns None if the file cannot be read at all.
    """
    entries = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 4:
                    entries.append((unescape_mountpoint(parts[1]), parts[2], parts[3]))
    except OSError as exc:
        print(f"disk_usage: cannot read {path}: {exc}", file=sys.stderr)
        return None
    return entries


def should_skip(mountpoint, fstype, options, skip_fstypes, excludes, skip_readonly):
    if mountpoint.startswith(SKIP_MOUNT_PREFIXES):
        return True
    if matches(skip_fstypes, fstype, fullmatch=True):
        return True
    if skip_readonly and "ro" in options.split(","):
        return True
    return matches(excludes, mountpoint, fullmatch=False)


def collect_metrics(entries, skip_fstypes, excludes, skip_readonly):
    metrics = {}
    for mountpoint, fstype, options in entries:
        if mountpoint in metrics:
            continue
        if should_skip(
            mountpoint, fstype, options, skip_fstypes, excludes, skip_readonly
        ):
            continue
        if not os.path.isdir(mountpoint):
            continue
        try:
            st = os.statvfs(mountpoint)
        except OSError:
            continue
        total = st.f_frsize * st.f_blocks
        free = st.f_frsize * st.f_bfree
        metrics[f"{mountpoint}:usage"] = (
            round(100.0 * (1.0 - free / total), 1) if total else 0.0
        )
    return metrics


def main():
    config = json.load(sys.stdin)
    skip_fstypes = compile_patterns(
        config.get("skip_fstypes", DEFAULT_SKIP_FSTYPES), fullmatch=True
    )
    excludes = compile_patterns(config.get("excludes", []), fullmatch=False)
    skip_readonly = bool(config.get("skip_readonly", True))

    mounts_path = os.environ.get("PYMON_DISK_MOUNTS", "/proc/mounts")
    entries = read_mounts(mounts_path)
    if entries is None:
        # A real failure: signal it via the exit code (agent maps != 0 to agent:error).
        sys.exit(1)

    print(json.dumps(collect_metrics(entries, skip_fstypes, excludes, skip_readonly)))


if __name__ == "__main__":
    main()
