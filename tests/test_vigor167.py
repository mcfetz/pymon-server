"""Redacted fixture tests for the Vigor 167 SSH/CLI collector."""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
spec = importlib.util.spec_from_file_location(
    "vigor167", os.path.join(ROOT, "plugins", "vigor167.py")
)
vigor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vigor)


# Real ``exec dslinfo`` output from a Vigor 167 (V5.2.9).  It is a summary:
# counters and attenuation come from the monitoring page below.
DSLINFO_TEXT = """
Status : Showtime

Mode : VDSL2

Profile : 17a

Annex : ANNEX B

DSL Version : 5.12.31.0_B_A60901

Line Uptime : 0d  1h  3m 30s

Downstream Line Rate : 116799 kbps

Upstream Line Rate : 42463 kbps

SNR Downstream : 7.8 dB

SNR Upstream : 11.5 dB
"""

# Shape of ``show all`` on
# ``config Monitoring DSL_Status Monitoring_DSL_General``.  Counter values are
# synthetic so the state-machine tests have deterministic deltas.
MONITORING = {
    "rid": "0000",
    "total": 1,
    "start": 0,
    "ct": [
        {
            "0MONITORING_DSL_GENERAL": [
                {
                    "Name": "Setting",
                    "Status": "Showtime",
                    "Mode": "VDSL2",
                    "Profile": "17a",
                    "Annex": "ANNEX B",
                    "DSL_Version": "5.12.31.0_B_A60901",
                    "Stream_Table": [
                        {
                            "Name": "Actual Rate",
                            "Downstream": "116799 kbps",
                            "Upstream": "42463 kbps",
                        },
                        {
                            "Name": "Attainable Rate",
                            "Downstream": "142147 kbps",
                            "Upstream": "50175 kbps",
                        },
                        {
                            "Name": "Path Mode",
                            "Downstream": "Interleaved",
                            "Upstream": "Fast",
                        },
                        {
                            "Name": "Interleave Depth",
                            "Downstream": "8",
                            "Upstream": "1",
                        },
                        {
                            "Name": "SNR Margin",
                            "Downstream": "7.7 dB",
                            "Upstream": "11.5 dB",
                        },
                    ],
                    "End_Table": [
                        {
                            "Name": "Attenuation",
                            "Near_End": "11.2 dB",
                            "Far_End": "4.1 dB",
                        },
                        {"Name": "CRC", "Near_End": "13", "Far_End": "6951"},
                        {"Name": "FECS", "Near_End": "-", "Far_End": "-"},
                        {"Name": "ES", "Near_End": "2 s", "Far_End": "1517 s"},
                        {"Name": "SES", "Near_End": "0 s", "Far_End": "0 s"},
                        {"Name": "LOSS", "Near_End": "-", "Far_End": "-"},
                        {"Name": "UAS", "Near_End": "0 s", "Far_End": "1722 s"},
                        {"Name": "HEC Errors", "Near_End": "0", "Far_End": "0"},
                        {
                            "Name": "RS Corrections",
                            "Near_End": "-",
                            "Far_End": "-",
                        },
                        {
                            "Name": "LOS Failure",
                            "Near_End": "0",
                            "Far_End": "0",
                        },
                        {
                            "Name": "LOF Failure",
                            "Near_End": "0",
                            "Far_End": "0",
                        },
                        {
                            "Name": "LPR Failure",
                            "Near_End": "0",
                            "Far_End": "0",
                        },
                        {
                            "Name": "NCD Failure",
                            "Near_End": "-",
                            "Far_End": "-",
                        },
                        {
                            "Name": "LCD Failure",
                            "Near_End": "0",
                            "Far_End": "0",
                        },
                        {"Name": "NFEC", "Near_End": "-", "Far_End": "-"},
                        {"Name": "RFEC", "Near_End": "10", "Far_End": "8"},
                        {"Name": "LYSMB", "Near_End": "-", "Far_End": "-"},
                    ],
                }
            ]
        }
    ],
}

MONITORING_JSON = json.dumps(MONITORING)

CLASSIC_TEXT = """
  ---------------------- ATU-R Info (hw: annex B, f/w: annex A/B/C) -----------
   Running Mode            :      17A       State                : SHOWTIME
   DS Actual Rate          : 109998000 bps   US Actual Rate       : 36997000 bps
   DS Attainable Rate      : 120000000 bps   US Attainable Rate   : 40000000 bps
   DS Path Mode            :  Interleave    US Path Mode         : Fast
   DS Interleave Depth     :      874       US Interleave Depth  : 1
   NE Current Attenuation  :       11 dB    Cur SNR Margin       : 12  dB
  -------------------------------- ATU-C Info ---------------------------------
   Far Current Attenuation :        4 dB    Far SNR Margin       : 9  dB
   NE CRC Count            :       13       FE CRC Count         : 6951
   NE ES Count             :        2       FE  ES Count         : 1517
"""

MORE_TEXT = """
  ---------------------- ATU-R Info (hw: annex B, f/w: annex A/B/C) -----------
   Running Mode            :      17A       State                : SHOWTIME
   DS Actual Rate          : 109998000 bps   US Actual Rate       : 36997000 bps
  ---------------------------- ATU-R Info II ----------------------------
                  Near End        Far End    Note
 Trellis      :      1               1
 FECS         :      0               0
 CRC          :     13            6951
 ES           :      2            1517
 SES          :      0               0
 UAS          :      0            1722
 LOS          :      0               0
 LOF          :      0               0
 LPR          :      0               0
 NCD          :      0               0
 LCD          :      0               0
"""


def _sample_parsed():
    parsed = vigor.parse_cli_output(DSLINFO_TEXT)
    vigor._merge_parsed(parsed, vigor.parse_monitoring_json(MONITORING_JSON))
    return parsed


def test_parse_exec_dslinfo_text():
    parsed = vigor.parse_cli_output(DSLINFO_TEXT)
    assert parsed["line_state"] == "SHOWTIME"
    assert parsed["mode"] == "VDSL2"
    assert parsed["profile"] == "17a"
    assert parsed["annex"] == "ANNEX B"
    assert parsed["dsl_version"] == "5.12.31.0_B_A60901"
    assert parsed["actual_rate"] == [116799, 42463]
    assert parsed["snr_margin"] == [7.8, 11.5]
    assert parsed["line_uptime_seconds"] == 3600 + 3 * 60 + 30
    # exec dslinfo is only the summary page.
    assert "crc" not in parsed
    assert "attenuation" not in parsed


def test_parse_monitoring_json():
    parsed = vigor.parse_monitoring_json(MONITORING_JSON)
    assert parsed["line_state"] == "SHOWTIME"
    assert parsed["mode"] == "VDSL2"
    assert parsed["attainable_rate"] == [142147, 50175]
    assert parsed["path_mode"] == ["Interleaved", "Fast"]
    assert parsed["interleave_depth"] == [8, 1]
    assert parsed["snr_margin"] == [7.7, 11.5]
    assert parsed["attenuation"] == [11.2, 4.1]
    assert parsed["crc"] == [13, 6951]
    assert parsed["es"] == [2, 1517]
    assert parsed["uas"] == [0, 1722]
    assert parsed["rfec"] == [10, 8]
    assert parsed["counter_direction"] == "near_end_far_end"


def test_parse_classic_vdsl_status_text():
    parsed = vigor.parse_cli_output(CLASSIC_TEXT)
    assert parsed["line_state"] == "SHOWTIME"
    # Classic firmware reports the VDSL profile in "Running Mode".
    assert parsed["profile"] == "17A"
    assert parsed["mode"] == "VDSL2"
    assert parsed["annex"] == "ANNEX B"
    assert parsed["actual_rate"] == [109998, 36997]
    assert parsed["attainable_rate"] == [120000, 40000]
    assert parsed["path_mode"] == ["Interleave", "Fast"]
    assert parsed["interleave_depth"] == [874, 1]
    assert parsed["snr_margin"] == [12, 9]
    assert parsed["attenuation"] == [11, 4]
    assert parsed["crc"] == [13, 6951]
    assert parsed["es"] == [2, 1517]
    assert parsed["counter_direction"] == "near_end_far_end"


def test_parse_vdsl_status_more_counters():
    parsed = vigor.parse_cli_output(MORE_TEXT)
    assert parsed["line_state"] == "SHOWTIME"
    assert parsed["fecs"] == [0, 0]
    assert parsed["crc"] == [13, 6951]
    assert parsed["es"] == [2, 1517]
    assert parsed["ses"] == [0, 0]
    assert parsed["uas"] == [0, 1722]
    assert parsed["los_failure"] == [0, 0]
    assert parsed["lof_failure"] == [0, 0]
    assert parsed["lpr_failure"] == [0, 0]
    assert parsed["ncd_failure"] == [0, 0]
    assert parsed["lcd_failure"] == [0, 0]


def test_parse_downstream_upstream_table():
    text = """
Status: SHOWTIME
Mode: VDSL2
                                Downstream      Upstream
Actual Rate                     109998 kbps     36997 kbps
SNR Margin                      12.1 dB         9.3 dB
CRC                             13              6
"""
    parsed = vigor.parse_cli_output(text)
    assert parsed["actual_rate"] == [109998, 36997]
    assert parsed["snr_margin"] == [12.1, 9.3]
    assert parsed["crc"] == [13, 6]
    assert parsed["counter_direction"] == "downstream_upstream"


def test_lookup_field_direction_and_long_labels():
    assert vigor._lookup_field("SNR_Downstream") == ("pair", "snr_margin", 0)
    assert vigor._lookup_field("CRC (Near End)") == ("counter", "crc", 0)
    assert vigor._lookup_field("Downstream_Line_Attenuation") == (
        "pair",
        "attenuation",
        0,
    )
    # "Mode" inside a longer label must not overwrite the DSL mode.
    assert vigor._lookup_field("Power Management Mode") is None


def test_merge_parsed_fills_counters_from_monitoring_json():
    parsed = vigor.parse_cli_output(DSLINFO_TEXT)
    monitoring = vigor.parse_monitoring_json(MONITORING_JSON)
    vigor._merge_parsed(parsed, monitoring)
    assert parsed["line_state"] == "SHOWTIME"
    assert parsed["mode"] == "VDSL2"
    assert parsed["line_uptime_seconds"] == 3600 + 3 * 60 + 30
    assert parsed["crc"] == [13, 6951]
    assert parsed["attenuation"] == [11.2, 4.1]
    assert parsed["counter_direction"] == "near_end_far_end"

    # A later page must never overwrite an already parsed line state.
    vigor._merge_parsed(parsed, {"line_state": "TRAINING", "mode": "ADSL2+"})
    assert parsed["line_state"] == "SHOWTIME"
    assert parsed["mode"] == "VDSL2"


def test_clean_cli_output_removes_prompt_and_echo():
    raw = "exec dslinfo\r\nStatus: SHOWTIME\r\nCRC: 13 6951\r\nvigor> "
    cleaned = vigor._clean_cli_output(raw, "exec dslinfo", "vigor>")
    assert "Status: SHOWTIME" in cleaned
    assert "CRC: 13 6951" in cleaned
    assert "vigor>" not in cleaned
    assert "exec dslinfo" not in cleaned


def test_prompt_detection_ignores_chipset_banner():
    assert vigor._PROMPT_RE.search("vigor>")
    assert vigor._PROMPT_RE.search("admin@Vigor167>")
    assert not vigor._PROMPT_RE.search("DSLAM CHIPSET VENDOR : < BDCM >")


def test_invalid_output_is_empty():
    assert vigor.parse_cli_output("") == {}
    assert vigor.parse_cli_output("garbage") == {}
    assert vigor.parse_monitoring_json("garbage") == {}


def test_compute_poll_keeps_compatible_metric_names():
    parsed = _sample_parsed()
    state = vigor._fresh_state()
    first = vigor.compute_poll(state, parsed, now=1000.0)
    assert first["vigor_dsl_scrape_success"] == 1
    assert first["vigor_dsl_line_state"] == "SHOWTIME"
    assert first["vigor_dsl_profile"] == "17a"
    assert first["vigor_dsl_annex"] == "ANNEX_B"
    assert first["vigor_dsl_version"] == "5.12.31.0_B_A60901"
    assert first["vigor_dsl_type"] == "VDSL2"
    assert first["vigor_dsl_running_mode"] == "VDSL2"
    assert first["vigor_dsl_path_mode_downstream"] == "Interleaved"
    assert first["vigor_dsl_path_mode_upstream"] == "Fast"
    assert first["vigor_dsl_attenuation_db_downstream"] == 11.2
    assert first["vigor_dsl_attenuation_db_upstream"] == 4.1
    assert first["vigor_dsl_sync_rate_kbps_downstream"] == 116799
    assert first["vigor_dsl_sync_rate_kbps_upstream"] == 42463
    assert first["vigor_dsl_attainable_rate_kbps_downstream"] == 142147
    assert first["vigor_dsl_attainable_rate_kbps_upstream"] == 50175
    assert first["vigor_dsl_crc_total_near_end"] == 13
    assert first["vigor_dsl_crc_total_far_end"] == 6951
    assert first["vigor_dsl_crc_total_downstream"] == 13
    assert first["vigor_dsl_crc_total_upstream"] == 6951
    assert first["vigor_dsl_line_uptime_seconds"] == 3600 + 3 * 60 + 30


def test_line_loss_and_recovery_are_distinct_from_transport_failure():
    parsed = _sample_parsed()
    state = vigor._fresh_state()
    vigor.compute_poll(state, parsed, now=1000.0)
    down = dict(parsed, line_state="TRAINING", actual_rate=[0, 0])
    lost = vigor.compute_poll(state, down, now=1030.0)
    assert lost["vigor_dsl_line_up"] == 0
    assert lost["vigor_dsl_resync_total"] == 0
    recovered = vigor.compute_poll(state, parsed, now=1432.0)
    assert recovered["vigor_dsl_line_up"] == 1
    assert round(recovered["vigor_dsl_last_outage_seconds"]) == 402

    failed = vigor.compute_poll(state, {}, now=1462.0)
    assert failed["vigor_dsl_scrape_success"] == 0
    assert "vigor_dsl_line_up" not in failed


def test_counter_delta_and_reset():
    parsed = _sample_parsed()
    state = vigor._fresh_state()
    vigor.compute_poll(state, parsed, now=1000.0)
    grown = dict(parsed)
    grown["crc"] = [23, 7001]
    second = vigor.compute_poll(state, grown, now=1960.0)
    assert second["vigor_dsl_crc_delta_near_end"] == 10
    assert second["vigor_dsl_crc_delta_far_end"] == 50
    assert second["vigor_dsl_crc_rate_perhour_near_end"] == 37.5

    reset = dict(grown)
    reset["crc"] = [1, 1]
    third = vigor.compute_poll(state, reset, now=2020.0)
    assert "vigor_dsl_crc_delta_near_end" not in third
    assert third["vigor_dsl_resync_total"] == 1


def test_schema_defaults_to_ssh():
    fields = {field["key"]: field for field in vigor.__schema__["fields"]}
    assert fields["sleep"]["default"] == 30
    assert fields["sleep"]["min"] == 30
    assert fields["ssh_port"]["default"] == 22
    assert fields["dsl_command"]["default"] == "exec dslinfo"
    assert "system_command" not in fields
    assert "port" not in fields
    assert "cgi_path" not in fields
    assert "http_encode" not in fields


def test_run_uses_ssh_cli_and_emits_metrics():
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self):
            pass

        def command(self, command, timeout=None):
            if command == "exec dslinfo":
                return DSLINFO_TEXT
            if command == vigor.MONITORING_PAGE_COMMAND:
                return ""
            if command == vigor.MONITORING_SHOW_COMMAND:
                return MONITORING_JSON
            return ""

        def close(self):
            pass

    original = vigor.Vigor167SSHClient
    state_dir = tempfile.mkdtemp(dir="/tmp/opencode")
    vigor.Vigor167SSHClient = FakeClient
    try:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            vigor.run(
                {
                    "host": "192.0.2.1",
                    "password": "pw",
                    "state_file": os.path.join(state_dir, "state.json"),
                }
            )
        metrics = json.loads(buffer.getvalue())
        assert metrics["vigor_dsl_scrape_success"] == 1
        assert metrics["vigor_dsl_monitoring_success"] == 1
        assert metrics["vigor_dsl_sync_rate_kbps_downstream"] == 116799
        assert metrics["vigor_dsl_crc_total_near_end"] == 13
        assert metrics["vigor_dsl_counter_columns"] == "near_end_far_end"
    finally:
        vigor.Vigor167SSHClient = original
        shutil.rmtree(state_dir, ignore_errors=True)


def test_run_without_config_exits_nonzero():
    """A run that cannot scrape must exit non-zero so agent:error is set."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        try:
            vigor.run({})
        except SystemExit as exc:
            assert exc.code == 1
        else:  # pragma: no cover - failure path
            raise AssertionError("run({}) must raise SystemExit(1)")
    assert "missing_config" in buffer.getvalue()


def main():
    """Run every test_* function; pytest is not installed in the venv."""
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
