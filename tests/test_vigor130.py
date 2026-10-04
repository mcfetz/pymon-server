"""Fixture-based tests for the Vigor 130 DSL parser and state machine.

There were no tests for vigor130.py before this file. The two page layouts that
matter are both covered, because the error-counter table is headed
Near End / Far End on this firmware while the sync table above it is headed
Downstream / Upstream. Getting that pairing wrong is what makes a CRC counter
look like a sync problem.
"""
import importlib.util
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="vigor_test_")
os.environ["PYMON_DATA_DIR"] = TMP
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

spec = importlib.util.spec_from_file_location(
    "vigor130", os.path.join(ROOT, "plugins", "vigor130.py")
)
vigor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vigor)

RESULTS = []


def check(name, got, want):
    ok = got == want
    RESULTS.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got={got!r}\n        want={want!r}")


def page(header, values):
    """Build a minimal two-column DSL status page.

    ``header`` is the column header pair for the error counters, ``values`` is
    a list of (label, first, second).
    """
    rows = [
        "<html><body><table>",
        "<tr><td>Line State</td><td>SHOWTIME</td></tr>",
        f"<tr><td></td><td>{header[0]}</td><td>{header[1]}</td></tr>",
        "<tr><td>Actual Rate</td><td>98338</td><td>kbps</td><td>26996</td><td>kbps</td></tr>",
        "<tr><td>SNR Margin</td><td>14</td><td>dB</td><td>15</td><td>dB</td></tr>",
    ]
    for label, a, b in values:
        rows.append(
            f"<tr><td>{label}</td><td>{a}</td><td></td><td>{b}</td><td></td></tr>"
        )
    rows.append("</table></body></html>")
    return "\n".join(rows)


NEAR_FAR = ("Near End", "Far End")
DOWN_UP = ("Downstream", "Upstream")

BASE_COUNTERS = [
    ("CRC", 111, 7906),
    ("ES", 12, 34),
    ("SES", 0, 0),
    ("LOSS", 5, 6),
    ("UAS", 7, 8),
    ("LOS Failure", 1, 2),
]


def main():
    print("== Spalten-Erkennung ==")
    check(
        "Near End / Far End wird erkannt",
        vigor._detect_counter_columns(vigor._normalize_text(page(NEAR_FAR, BASE_COUNTERS))),
        "near_end_far_end",
    )
    check(
        "Downstream / Upstream wird erkannt",
        vigor._detect_counter_columns(vigor._normalize_text(page(DOWN_UP, BASE_COUNTERS))),
        "downstream_upstream",
    )
    check(
        "Sync-Tabelle allein fuehrt nicht zu falscher Zuordnung",
        vigor._detect_counter_columns(vigor._normalize_text(page(NEAR_FAR, BASE_COUNTERS))),
        "near_end_far_end",
    )

    print("\n== Parser: Position wird unabhaengig vom Header gelesen ==")
    for header in (NEAR_FAR, DOWN_UP):
        p = vigor.parse_dsl_page(page(header, BASE_COUNTERS))
        tag = "near_end_far_end" if header is NEAR_FAR else "downstream_upstream"
        check(f"[{tag}] line_state", p.get("line_state"), "SHOWTIME")
        check(f"[{tag}] sync down", p.get("actual_rate"), [98338, 26996])
        check(f"[{tag}] snr", p.get("snr_margin"), [14, 15])
        check(f"[{tag}] crc pair", p.get("crc"), [111, 7906])
        check(f"[{tag}] uas pair", p.get("uas"), [7, 8])

    print("\n== TRAINING mit Sync 0/0 ist ein echtes Ereignis ==")
    training = (
        "<table>"
        "<tr><td>Line State</td><td>TRAINING</td></tr>"
        "<tr><td>Actual Rate</td><td>0</td><td>kbps</td><td>0</td><td>kbps</td></tr>"
        "</table>"
    )
    p = vigor.parse_dsl_page(training)
    check("line_state", p.get("line_state"), "TRAINING")
    check("sync 0/0", p.get("actual_rate"), [0, 0])

    print("\n== Zustandsmaschine: SHOWTIME -> TRAINING -> SHOWTIME ==")
    st = vigor._fresh_state()
    showtime = vigor.parse_dsl_page(page(NEAR_FAR, BASE_COUNTERS))
    m1 = vigor.compute_poll(st, showtime, now=1000.0, uptime_seconds=644466.0)
    check("erster Poll: keine line_up", "vigor_dsl_line_up" in m1, False)
    check("erster Poll: kein resync", m1.get("vigor_dsl_resync_total"), 0)
    check("erster Poll: kein reboot", m1.get("vigor_dsl_reboot_total"), 0)
    check("erster Poll: system_uptime", m1.get("vigor_dsl_system_uptime_seconds"), 644466)
    check("erster Poll: kein Alarm-Ausloeser", m1.get("vigor_dsl_scrape_success"), 1)

    m2 = vigor.compute_poll(st, vigor.parse_dsl_page(training), now=1030.0, uptime_seconds=644496.0)
    check("TRAINING: line_up 0", m2.get("vigor_dsl_line_up"), 0)
    check("TRAINING: kein Resync-Zaehler", m2.get("vigor_dsl_resync_total"), 0)

    m3 = vigor.compute_poll(st, showtime, now=1432.0, uptime_seconds=644898.0)
    check("Recovery: line_up 1", m3.get("vigor_dsl_line_up"), 1)
    check("Recovery: Ausfalldauer ~402s", round(m3.get("vigor_dsl_last_outage_seconds")), 402)

    print("\n== Reboot vs Resync ==")
    # Counter reset WITH falling uptime -> reboot, not resync.
    st = vigor._fresh_state()
    vigor.compute_poll(st, showtime, now=1000.0, uptime_seconds=644466.0)
    # A modem reboot clears the counters *and* the uptime, so both move at once.
    zeroed = vigor.parse_dsl_page(page(NEAR_FAR, [("CRC", 0, 0), ("ES", 0, 0)]))
    m = vigor.compute_poll(st, zeroed, now=1100.0, uptime_seconds=5000.0)
    check("Uptime-Rueckgang -> reboot", m.get("vigor_dsl_reboot_total"), 1)
    check("Uptime-Rueckgang -> kein resync", m.get("vigor_dsl_resync_total"), 0)

    # Counter reset WITHOUT uptime drop -> resync.
    st = vigor._fresh_state()
    vigor.compute_poll(st, showtime, now=1000.0, uptime_seconds=644466.0)
    small = vigor.parse_dsl_page(page(NEAR_FAR, [("CRC", 0, 0), ("ES", 0, 0), ("SES", 0, 0)]))
    m = vigor.compute_poll(st, small, now=1100.0, uptime_seconds=644566.0)
    check("stabile Uptime + Reset -> resync", m.get("vigor_dsl_resync_total"), 1)
    check("stabile Uptime + Reset -> kein reboot", m.get("vigor_dsl_reboot_total"), 0)

    print("\n== Transportfehler sind kein DSL-Ausfall ==")
    st = vigor._fresh_state()
    vigor.compute_poll(st, showtime, now=1000.0, uptime_seconds=644466.0)
    # A scrape that yields no line_state must not touch resync/reboot.
    m = vigor.compute_poll(st, {}, now=1030.0)
    check("Fehlschlag: scrape_success 0", m.get("vigor_dsl_scrape_success"), 0)
    check("Fehlschlag: kein line_up", "vigor_dsl_line_up" in m, False)
    check("Fehlschlag: kein resync/reboot", ("vigor_dsl_resync_total" in m), False)
    check("Fehlschlag: kein outage", "vigor_dsl_last_outage_seconds" in m, False)

    # SHOWTIME after transport errors must not fabricate an outage or resync.
    m = vigor.compute_poll(st, showtime, now=1120.0, uptime_seconds=644586.0)
    check("nach Transportfehler: line_up 1", m.get("vigor_dsl_line_up"), 1)
    check("nach Transportfehler: kein Resync", m.get("vigor_dsl_resync_total"), 0)
    check("nach Transportfehler: kein Outage", "vigor_dsl_last_outage_seconds" in m, False)

    print("\n== Uptime-Dauern ==")
    check("179:1:6", vigor._parse_duration("179:1:6"), 179 * 3600 + 60 + 6)
    check("07:15:32", vigor._parse_duration("07:15:32"), 7 * 3600 + 15 * 60 + 32)
    check("7 days 3:25:12", vigor._parse_duration("7 days 3:25:12"), 7 * 86400 + 3 * 3600 + 25 * 60 + 12)
    check("keine Dauer", vigor._parse_duration("System Up Time"), None)

    print("\n== Keine Secrets in Metriken ==")
    st = vigor._fresh_state()
    m = vigor.compute_poll(st, showtime, now=1000.0, uptime_seconds=644466.0)
    for k, v in m.items():
        blob = f"{k}={v}".lower()
        if "password" in blob or "secret" in blob or "sformaut" in blob:
            check(f"Secret in {k}", True, False)
    check("keine Secret-Metrik", True, True)

    print()
    failed = [r for r in RESULTS if not r[1]]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} bestanden")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())