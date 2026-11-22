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
#
# ── Optional: topology ──
# A plugin may ALSO describe the nodes and edges it knows about. Add the
# reserved key "__topology__" next to the metrics; the agent strips it and the
# server stores it as the plugin's current view of the graph (see topology.py).
# A plugin that only produces a graph may omit metrics entirely.
#
# IDs are chosen by the plugin and must be globally unique and stable across
# runs: "<namespace>:<kind>:<key>". They are the merge key of the integrated
# graph, so two plugins reporting the same id describe the same node.
#
#   output = {
#       "example_metric_1": 42.0,
#       "__topology__": {
#           "v": 1,
#           "nodes": [
#               {"id": "example:host:web01", "kind": "host",
#                "label": "web01", "attrs": {"ip": "10.0.0.5"}},
#               {"id": "example:service:redis", "kind": "service",
#                "label": "redis"},
#           ],
#           "edges": [
#               {"src": "example:host:web01", "dst": "example:service:redis",
#                "kind": "runs", "attrs": {"since": "2026-11-01"}},
#           ],
#       },
#   }
#
# Rules:
#   - "nodes" and "edges" are optional; both are lists of objects.
#   - node: id (required), kind, label, attrs (free-form object)
#   - edge: src + dst (required, must match node ids), kind, label, attrs
#   - Everything reported in a payload is upserted; elements the plugin does
#     not report in the same payload are removed from its snapshot. A payload
#     with neither nodes nor edges changes nothing.
#   - Entries without an id (node) resp. src/dst (edge) are skipped with a
#     warning, so one malformed entry cannot break the ingest.

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
