# pymon-server - Development Guidelines

## Stack
- Flask + SQLAlchemy + SQLite
- Python 3.14, ruff

## Mandatory checks
- venv/bin/ruff check . → 0 errors
- venv/bin/ruff format --check . → 0 changes
  - Hinweis (2026-10): 49 vorbestehende Dateien sind unformatiert (uncommitteter
    Stand, bewusst nicht angefasst). Neue/geänderte Dateien selbst formatiert
    halten: venv/bin/ruff format <datei>
- venv/bin/python tests/test_count_ratio.py tests/test_ucg_eth4.py tests/test_vigor130.py → 8/8 bestanden
- venv/bin/python tests/test_vigor167.py → 11/11 bestanden
- venv/bin/python /tmp/opencode/test_homey_rules.py → 34/34 bestanden
