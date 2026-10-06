# pymon-server - Development Guidelines

## Stack
- Flask + SQLAlchemy + SQLite
- Python 3.14, ruff

## Mandatory checks
- venv/bin/ruff check . → 0 errors
- venv/bin/ruff format --check . → 0 changes
- venv/bin/python tests/test_count_ratio.py tests/test_ucg_eth4.py tests/test_vigor130.py → 8/8 bestanden
- venv/bin/python /tmp/opencode/test_homey_rules.py → 34/34 bestanden
