#!/usr/bin/env python3
"""{name}.py — Description of your plugin. No external deps."""
import json
import sys

# ── Schema ──
# Defines which fields are shown in the config UI.
# Supported field types:
#   "number"       — integer/float input
#   "string"       — text input
#   "boolean"      — checkbox
#   "array:string" — list of strings with +/− buttons
#   "array:object" — list of objects with sub-fields
__schema__ = {
    "label": "{name}",
    "description": "Description of your plugin",
    "fields": [
        {
            "key": "sleep",
            "label": "Interval (s)",
            "type": "number",
            "default": 60,
            "min": 5,
        },
        {
            "key": "api_url",
            "label": "API URL",
            "type": "string",
            "default": "https://example.com/api",
            "optional": True,
        },
        {
            "key": "verbose",
            "label": "Verbose logging",
            "type": "boolean",
            "default": False,
            "optional": True,
        },
        {
            "key": "hosts",
            "label": "Host list",
            "type": "array:string",
            "default": ["host1", "host2"],
        },
        {
            "key": "urls",
            "label": "URL checks",
            "type": "array:object",
            "default": [],
            "fields": [
                {"key": "name", "label": "Name", "type": "string"},
                {"key": "url", "label": "URL", "type": "string"},
                {
                    "key": "timeout",
                    "label": "Timeout (s)",
                    "type": "number",
                    "default": 5,
                    "optional": True,
                },
            ],
        },
    ],
}

# ── Plugin logic ──
# Config is read from stdin as JSON.
# Output must be a single JSON object with flat key-value pairs.
# Keys become metric names, values must be numbers, strings,
# or booleans.
# Example: {"cpu_percent": 45.2, "status": "ok"}

if __name__ == "__main__":
    config = json.load(sys.stdin)

    sleep = config.get("sleep", 60)  # noqa: F841
    api_url = config.get("api_url", "")
    verbose = config.get("verbose", False)  # noqa: F841
    hosts = config.get("hosts", [])  # noqa: F841
    url_checks = config.get("urls", [])  # noqa: F841

    # Your monitoring logic here
    # ...

    # Return metrics as flat JSON
    output = {
        "example_metric_1": 42.0,
        "example_status": "running",
    }
    print(json.dumps(output))
