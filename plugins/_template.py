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
# A node carries the set of GENERIC identifiers it is known by - never a
# plugin-private key, because nothing else could ever match it. Report every
# identifier you can get cheaply (for a host: MAC, IP and name):
#
#   mac:aa:bb:cc:dd:ee:ff        lowercased, colon separated
#   ip:192.168.181.22            IPv4
#   host:docker12.srv            fully qualified name, lowercased
#   endpoint:docker12.srv:8123   name or IPv4 plus a port (services, not hosts)
#
# Two nodes belong to the same device when their identifier sets overlap, and
# the server merges transitively - so a switch knows a device by MAC while
# docker knows it by IP, and the graph still shows one node:
#
#   unifi_topology:  mac:aa:bb… + ip:192.168.179.22
#   docker_host:                  ip:192.168.179.22 + host:docker12.srv
#
#   output = {
#       "example_metric_1": 42.0,
#       "__topology__": {
#           "v": 2,
#           "nodes": [
#               {"ids": ["host:web01.lan", "ip:10.0.0.5"], "kind": "host",
#                "label": "web01", "attrs": {"role": "web"}},
#               {"ids": ["endpoint:web01.lan:5432"], "kind": "service",
#                "label": "postgres"},
#           ],
#           "edges": [
#               {"src": "host:web01.lan", "dst": "endpoint:web01.lan:5432",
#                "kind": "runs", "attrs": {"since": "2026-11-01"}},
#           ],
#       },
#   }
#
# Rules:
#   - "nodes" and "edges" are optional; both are lists of objects.
#   - node: ids (required, list; a single "id" string is accepted too),
#           kind, label, attrs (free-form object)
#   - edge: src + dst (required, any identifier a node declares), kind,
#           label, attrs
#   - Everything reported in a payload is upserted; elements the plugin does
#     not report in the same payload are removed from its snapshot. A payload
#     with neither nodes nor edges changes nothing.
#   - Entries without a usable identifier (node) resp. src/dst (edge) are
#     skipped with a warning, so one malformed entry cannot break the ingest.
#   - An edge may name an endpoint nothing described; it becomes a node of its
#     own (marked implicit), so no edge ever dangles.

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
