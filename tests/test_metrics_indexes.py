"""Regression guard for the metrics index set.

The live database carried 8.8 GB of indexes on a 25.6 M row table, three of
which were either exact duplicates of an existing index or prefixes that bought
no measurable plan improvement (see
migrations/2026-11-19-drop-redundant-metrics-indexes.sql). This test keeps the
redundant ones from creeping back into the ORM definition.
"""

import os
import sys
import tempfile

os.environ["PYMON_DATA_DIR"] = tempfile.mkdtemp(prefix="pymon_metrics_indexes_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db_models import Metrics  # noqa: E402


def _index_names() -> set[str]:
    return {index.name for index in Metrics.__table__.indexes}


def test_id_is_not_indexed_twice():
    """PRIMARY KEY already creates a btree on id; index=True added a second one."""
    assert "ix_metrics_id" not in _index_names()


def test_prefix_index_on_plugin_metric_is_gone():
    assert "idx_metrics_plugin_metric" not in _index_names()


def test_query_critical_indexes_are_still_declared():
    names = _index_names()
    # Ingest dedup, rule evaluation, charts/cleanup respectively.
    assert "idx_metrics_agent_plugin_metric_ts" in names
    assert "idx_metrics_agent_plugin_ts" in names
    assert "idx_metrics_timestamp" in names


def main():
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
    sys.exit(main())
