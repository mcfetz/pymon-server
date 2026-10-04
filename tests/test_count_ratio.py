"""Regression tests for count_ratio violation reconstruction.

The motivating incident (2026-10-04, DSL retrain) produced four alarms whose
message showed a perfectly healthy value:

    17:45:39 [vigsnrup] vigor_dsl_snr_db_upstream = 15.0   (rule is `lt 10`)

15 does not violate `lt 10`. The trigger was the stale `0` row from the
outage, stretched across the whole window by the reconstruction.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

TMP = tempfile.mkdtemp(prefix="pymon_test_")
os.environ["PYMON_DATA_DIR"] = TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from db_models import Base, Metrics  # noqa: E402
from rules import _count_ratio_violations  # noqa: E402


class _Rule:
    """Minimal stand-in for the Rule model."""

    def __init__(self, condition="lt", threshold=10.0, operator=None, agentid=None):
        self.id = "test"
        self.condition = condition
        self.threshold = threshold
        self.operator = operator or condition
        self.agentid = agentid
        self.unit = None
        self.max_value = None
        self.min_value = None


def _fresh_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False)()


def _add(session, metric, value, ts, agentid="a1", pluginid="p1"):
    session.add(
        Metrics(
            agentid=agentid,
            pluginid=pluginid,
            metric=metric,
            timestamp=ts,
            value_float=float(value),
        )
    )
    session.commit()


def _run(session, metric, rule, window, sleep, now):
    from sqlalchemy import and_

    base = (Metrics.agentid == "a1", Metrics.pluginid == "p1", Metrics.metric == metric)
    return _count_ratio_violations(session, base, rule, "a1", window, sleep, now)


RESULTS = []


def check(name, got, want):
    ok = got == want
    RESULTS.append((name, ok, got, want))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got={got!r} want={want!r}")


def main():
    m = "vigor_dsl_snr_db_upstream"
    sleep = 30.0
    window = 5
    rule = _Rule(condition="lt", threshold=10.0)

    # ---- The incident, verbatim ----
    # 17:38:40 -> TRAINING, SNR 0.  404s of outage with the plugin not
    # emitting this metric at all.  17:45:24 -> SHOWTIME, SNR 15 (healthy).
    # Alarm was evaluated at 17:45:39.
    t0 = datetime(2026, 10, 4, 17, 38, 40, tzinfo=timezone.utc)
    t1 = datetime(2026, 10, 4, 17, 45, 24, tzinfo=timezone.utc)
    now = datetime(2026, 10, 4, 17, 45, 39, tzinfo=timezone.utc)

    s = _fresh_session()
    _add(s, m, 0, t0)
    _add(s, m, 15, t1)
    v, val = _run(s, m, rule, window, sleep, now)
    check("incident: stale 0 must NOT fill the window", v, 0)
    check("incident: no violating value reported", val, None)

    # ---- A genuine violation still fires ----
    # Healthy 15s, then a real sustained dip to 5 with the value actually
    # changing (so dedup stores a row per change), then recovery. Evaluation
    # happens 15s after the newest row, matching the observed 17:45:24 -> 17:45:39.
    s = _fresh_session()
    base = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)
    for i in range(4):
        _add(s, m, 5, base + timedelta(seconds=30 * i))   # 4 violating polls
    _add(s, m, 15, base + timedelta(seconds=120))          # recovered
    v, val = _run(s, m, rule, window, sleep, base + timedelta(seconds=135))
    check("real sustained violation still detected", v >= 4, True)
    check("violating value is the offending one, not newest", val, 5.0)

    # ---- Same shape but only 3 of 5 violating: must stay silent ----
    # Guards against the fix over-suppressing real alarms.
    s = _fresh_session()
    for i in range(3):
        _add(s, m, 5, base + timedelta(seconds=30 * i))
    _add(s, m, 15, base + timedelta(seconds=90))
    v, _ = _run(s, m, rule, window, sleep, base + timedelta(seconds=105))
    check("3 of 5 violating stays below min_violations=4", v < 4, True)

    # ---- Heartbeat cadence keeps working ----
    # Stable value with heartbeat rows every 60s across a 5 poll window.
    s = _fresh_session()
    for i in range(5):
        _add(s, m, 15, base + timedelta(seconds=60 * i))
    v, val = _run(s, m, rule, window, sleep, base + timedelta(seconds=300))
    check("healthy heartbeat history yields 0 violations", v, 0)

    # ---- A single genuinely old row outside the window is ignored ----
    s = _fresh_session()
    _add(s, m, 5, base)                                  # 10 min old
    _add(s, m, 15, base + timedelta(seconds=600))        # newest, healthy
    v, val = _run(s, m, rule, window, sleep, base + timedelta(seconds=615))
    check("row beyond window span contributes nothing", v, 0)

    # ---- Rule boundary is respected (15 >= 10 is not a violation) ----
    s = _fresh_session()
    for i in range(4):
        _add(s, m, 15, base + timedelta(seconds=30 * i))
    v, _ = _run(s, m, rule, window, sleep, base + timedelta(seconds=120))
    check("value exactly at threshold is not a violation", v, 0)

    print()
    failed = [r for r in RESULTS if not r[1]]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} bestanden")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())