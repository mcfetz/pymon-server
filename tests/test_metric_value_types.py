"""Regression tests for PostgreSQL-safe metric value handling."""

import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta

TMP = tempfile.mkdtemp(prefix="pymon_value_types_")
os.environ["PYMON_DATA_DIR"] = TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from db_models import Base, Metrics  # noqa: E402
from functions import dict_value_to_metric  # noqa: E402
from rules import Rule, evaluate_single_rule  # noqa: E402


def test_bool_is_stored_as_integer():
    assert dict_value_to_metric(True, Metrics()).value_int == 1
    assert dict_value_to_metric(False, Metrics()).value_int == 0
    assert dict_value_to_metric(7, Metrics()).value_int == 7
    assert dict_value_to_metric(1.5, Metrics()).value_float == 1.5


def _fresh_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False)()


def test_moving_average_query_runs():
    session = _fresh_session()
    now = datetime.now(UTC)
    for index, value in enumerate((1.0, 2.0, 3.0)):
        session.add(Metrics(
            agentid="a1", pluginid="p1", metric="m",
            timestamp=now + timedelta(seconds=index), value_float=value,
        ))
    session.commit()
    trigger = (
        session.query(Metrics)
        .order_by(Metrics.timestamp.desc())
        .first()
    )
    rule = Rule(
        id="r", title="", enabled=True, description="", pluginid="p1",
        metric="m", condition="gt", threshold=100.0,
        scope="moving_avg", window_size=3,
    )
    # This used to compile to an aggregate with ORDER BY, which PostgreSQL
    # rejects; it must execute cleanly (and not fire at threshold 100).
    evaluate_single_rule(session, "a1", "p1", "m", rule, trigger, [])


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
