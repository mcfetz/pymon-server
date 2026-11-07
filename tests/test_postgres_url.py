"""The documented bare postgresql:// URL must resolve to the shipped driver."""

import os
import subprocess
import sys
import tempfile


def _read_config(url):
    env = os.environ.copy()
    env["PYMON_DATA_DIR"] = tempfile.mkdtemp(prefix="pymon_pg_url_")
    env["PYMON_DATABASE_URL"] = url
    out = subprocess.check_output(
        [
            sys.executable,
            "-c",
            "import config; print(config.DATABASE_URL); print(config.IS_POSTGRES)",
        ],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env,
        text=True,
    )
    return out.strip().splitlines()


def test_bare_postgres_url_uses_psycopg2():
    normalized, is_postgres = _read_config("postgresql://u:p@db:5432/pymon")
    assert normalized == "postgresql+psycopg2://u:p@db:5432/pymon"
    assert is_postgres == "True"


def test_explicit_driver_url_is_unchanged():
    normalized, is_postgres = _read_config(
        "postgresql+psycopg2://u:p@db:5432/pymon"
    )
    assert normalized == "postgresql+psycopg2://u:p@db:5432/pymon"
    assert is_postgres == "True"


def _main():
    tests = [
        (name, value)
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    failed = []
    for name, test in tests:
        try:
            test()
            print(f"  PASS  {name}")
        except Exception as exc:  # noqa: BLE001 - report, do not abort the run
            failed.append(name)
            print(f"  FAIL  {name}: {exc}")
    print()
    print(f"{len(tests) - len(failed)}/{len(tests)} bestanden")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
