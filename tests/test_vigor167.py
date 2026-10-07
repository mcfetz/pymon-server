"""Redacted fixture tests for the Vigor 167 JSON/CGI collector."""

import hashlib
import importlib.util
import os
import ssl
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
spec = importlib.util.spec_from_file_location(
    "vigor167", os.path.join(ROOT, "plugins", "vigor167.py")
)
vigor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vigor)


STATUS = {
    "rid": "0000",
    "ct": [
        {
            "1DSL_STS_INFO_LITE": [
                {
                    "Name": "1",
                    "Status": "Showtime",
                    "Mode": "VDSL2",
                    "Profile": "17a",
                    "Annex": "ANNEX B",
                    "DSL_Version": "5.12.31.0_B_A60901",
                    "Line_Uptime": "0d 3h 4m 5s",
                    "Downstream_Line_Rate": "109998 kbps",
                    "Upstream_Line_Rate": "36997 kbps",
                    "SNR_Downstream": "12.1 dB",
                    "SNR_Upstream": "9.3 dB",
                }
            ],
            "0MONITORING_DSL_GENERAL": [
                {
                    "Name": "Setting",
                    "Status": "Showtime",
                    "Mode": "VDSL2",
                    "Profile": "17a",
                    "Stream_Table": [
                        {"Name": "CRC", "Downstream": "13", "Upstream": "6"},
                        {"Name": "ES", "Downstream": "2", "Upstream": "7"},
                        {
                            "Name": "Path Mode",
                            "Downstream": "Interleave",
                            "Upstream": "Fast",
                        },
                    ],
                    "End_Table": [
                        {"Name": "CRC", "Near_End": "13", "Far_End": "6951"},
                        {"Name": "ES", "Near_End": "2", "Far_End": "1517"},
                        {"Name": "UAS", "Near_End": "0", "Far_End": "1722"},
                        {
                            "Name": "Attenuation",
                            "Near_End": "11.2 dB",
                            "Far_End": "4.1 dB",
                        },
                    ],
                }
            ],
        }
    ],
}

# Keep the response fixtures independent: the real collector receives these
# blocks from separate op=501 calls.
STATUS_ONLY = {
    "rid": "0000",
    "ct": [{"1DSL_STS_INFO_LITE": STATUS["ct"][0]["1DSL_STS_INFO_LITE"]}],
}
MONITORING_ALL = {
    "rid": "0000",
    "ct": [{"0MONITORING_DSL_GENERAL": STATUS["ct"][0]["0MONITORING_DSL_GENERAL"]}],
}
MONITORING_ONLY = {
    "rid": "0000",
    "ct": [
        {
            "0MONITORING_DSL_GENERAL": [
                {
                    "Name": "Setting",
                    "Status": "Showtime",
                    "Mode": "VDSL2",
                    "Profile": "17a",
                }
            ]
        }
    ],
}

SYSTEM = {
    "rid": "0000",
    "ct": [{"1SYSTEM_INFO": [{"System_Uptime": "2d 4h 5m 6s"}]}],
}


def test_extract_status_and_metadata():
    parsed = vigor.parse_api_responses(STATUS_ONLY, MONITORING_ALL, SYSTEM)
    assert parsed["line_state"] == "SHOWTIME"
    assert parsed["mode"] == "VDSL2"
    assert parsed["profile"] == "17a"
    assert parsed["annex"] == "ANNEX B"
    assert parsed["dsl_version"] == "5.12.31.0_B_A60901"
    assert parsed["actual_rate"] == [109998, 36997]
    assert parsed["snr_margin"] == [12.1, 9.3]
    assert parsed["line_uptime_seconds"] == 3 * 3600 + 4 * 60 + 5
    assert parsed["system_uptime_seconds"] == 2 * 86400 + 4 * 3600 + 5 * 60 + 6


def test_plain_second_uptimes_from_drayos5():
    # The real Vigor 167 reports System_Uptime as an integer (2681), not "2d 4h".
    system = {"rid": "0000", "ct": [{"1SYSTEM_INFO": [{"System_Uptime": 2681}]}]}
    parsed = vigor.parse_api_responses(STATUS_ONLY, MONITORING_ALL, system)
    assert parsed["system_uptime_seconds"] == 2681
    assert vigor._to_duration(2681) == 2681
    assert vigor._to_duration(2681.0) == 2681
    assert vigor._to_duration(True) is None
    assert vigor._to_duration("0d  0h 38m 46s") == 38 * 60 + 46


def test_preserve_near_far_and_parse_error_tables():
    parsed = vigor.parse_api_responses(STATUS_ONLY, MONITORING_ALL, SYSTEM)
    assert parsed["crc"] == [13, 6951]
    assert parsed["es"] == [2, 1517]
    assert parsed["uas"] == [0, 1722]
    assert parsed["counter_direction"] == "near_end_far_end"
    assert parsed["path_mode"] == ["Interleave", "Fast"]
    assert parsed["attenuation"] == [11.2, 4.1]


def test_compute_poll_keeps_compatible_metric_names():
    parsed = vigor.parse_api_responses(STATUS_ONLY, MONITORING_ALL, SYSTEM)
    state = vigor._fresh_state()
    first = vigor.compute_poll(state, parsed, now=1000.0)
    assert first["vigor_dsl_scrape_success"] == 1
    assert first["vigor_dsl_line_state"] == "SHOWTIME"
    assert first["vigor_dsl_profile"] == "17a"
    assert first["vigor_dsl_annex"] == "ANNEX_B"
    assert first["vigor_dsl_version"] == "5.12.31.0_B_A60901"
    assert first["vigor_dsl_type"] == "VDSL2"
    assert first["vigor_dsl_running_mode"] == "VDSL2"
    assert first["vigor_dsl_path_mode_downstream"] == "Interleave"
    assert first["vigor_dsl_path_mode_upstream"] == "Fast"
    assert first["vigor_dsl_attenuation_db_downstream"] == 11.2
    assert first["vigor_dsl_attenuation_db_upstream"] == 4.1
    assert first["vigor_dsl_sync_rate_kbps_downstream"] == 109998
    assert first["vigor_dsl_sync_rate_kbps_upstream"] == 36997
    assert first["vigor_dsl_crc_total_near_end"] == 13
    assert first["vigor_dsl_crc_total_far_end"] == 6951
    assert first["vigor_dsl_crc_total_downstream"] == 13
    assert first["vigor_dsl_crc_total_upstream"] == 6951
    assert first["vigor_dsl_line_uptime_seconds"] == 3 * 3600 + 4 * 60 + 5


def test_line_loss_and_recovery_are_distinct_from_transport_failure():
    parsed = vigor.parse_api_responses(STATUS_ONLY, MONITORING_ALL, SYSTEM)
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
    parsed = vigor.parse_api_responses(STATUS_ONLY, MONITORING_ALL, SYSTEM)
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


def test_login_payload_hash_and_ct_encoding():
    payload = vigor.build_login_payload("admin", "pw", utc=123)
    assert payload["ct"][0]["Name"] == "admin"
    assert payload["ct"][0]["Password"] == hashlib.sha512(b"pw").hexdigest()
    assert len(payload["ct"][0]["Password"]) == 128
    body = vigor.build_cgi_body("event", "552", payload)
    assert body.startswith("pid=event&op=552&ct=")
    encoded_ct = body.split("&ct=", 1)[1]
    assert encoded_ct[0] in "012"
    assert vigor.decode_cgi_response(encoded_ct) == payload
    assert "%22Password%22" not in body
    assert "pw" not in body

    legacy_body = vigor.build_cgi_body("event", "552", payload, http_encode=False)
    assert "%22Password%22" in legacy_body
    assert "pw" not in legacy_body


def test_https_defaults_to_certificate_verification():
    client = vigor.Vigor167Client("https://192.0.2.1", timeout=1)
    handler = next(
        item
        for item in client.opener.handlers
        if isinstance(item, urllib.request.HTTPSHandler)
    )
    context = getattr(handler, "_context")
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_separate_pid_responses_are_merged():
    status = STATUS_ONLY
    monitoring = MONITORING_ONLY
    stream = {
        "rid": "0000",
        "ct": [
            {
                "1MON_DSL_STREAM_TABLE": STATUS["ct"][0]["0MONITORING_DSL_GENERAL"][0][
                    "Stream_Table"
                ]
            }
        ],
    }
    end = {
        "rid": "0000",
        "ct": [
            {
                "1MON_DSL_END_TABLE": STATUS["ct"][0]["0MONITORING_DSL_GENERAL"][0][
                    "End_Table"
                ]
            }
        ],
    }
    merged = vigor._merge_responses(monitoring, stream, end)
    parsed = vigor.parse_api_responses(status, merged, SYSTEM)
    assert parsed["crc"] == [13, 6951]
    assert parsed["counter_direction"] == "near_end_far_end"


def test_invalid_api_response_is_rejected():
    assert vigor.parse_api_responses({"rid": "2000", "ct": []}, {}) == {}


def test_schema_defaults_to_thirty_second_polling():
    fields = {field["key"]: field for field in vigor.__schema__["fields"]}
    assert fields["sleep"]["default"] == 30
    assert fields["sleep"]["min"] == 30
    assert fields["http_encode"]["default"] is True


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
