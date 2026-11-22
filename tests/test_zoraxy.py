#!/usr/bin/env python3
"""test_zoraxy.py — Topologie des zoraxy-Plugins.

Eigenstaendiges Skript (kein pytest), wie die uebrigen Tests: druckt
"x/y bestanden" und endet mit 0 oder 1.

Der HTTP-Zugriff wird durch einen Fake-Opener ersetzt; getestet wird, was aus
einer Regel-Liste im Graphen wird - vor allem, dass ein Upstream "host:port" zum
endpoint-Identifier wird, denn darauf zeigen die Docker-Knoten.
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "plugins"))
spec = importlib.util.spec_from_file_location("zoraxy", os.path.join(HERE, "..", "plugins", "zoraxy.py"))
zoraxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zoraxy)

CHECKS = []


def check(name, got, want):
    CHECKS.append((name, got == want, f"{got!r} != {want!r}" if got != want else ""))


def check_true(name, cond, detail=""):
    CHECKS.append((name, bool(cond), "" if cond else detail))


class Response:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text.encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Answer /api/proxy/list with the given rules and log every request."""

    def __init__(self, rules, login_html='<meta name="zoraxy.csrf.Token" content="tok123">'):
        self.rules = rules
        self.login_html = login_html
        self.requests = []

    def open(self, req, timeout=None):
        url = req.full_url
        self.requests.append((url, dict(req.headers), req.data))
        if "/api/proxy/list" in url:
            return Response(json.dumps(self.rules))
        if "checkLogin" in url:
            return Response("true")
        return Response(self.login_html)


def _rule(name, origins, disabled=False, vdirs=None, auth=0):
    return {
        'RootOrMatchingDomain': name,
        'Disabled': disabled,
        'ActiveOrigins': [{'OriginIpOrDomain': o, 'RequireTLS': False, 'Weight': 1} for o in origins],
        'InactiveOrigins': [],
        'AuthenticationProvider': {'AuthMethod': auth},
        'VirtualDirectories': vdirs or [],
    }


def case_endpoint_parsing():
    check("bare host:port", zoraxy._endpoint_of("docker11.srv:8123"), "docker11.srv:8123")
    check("http prefix", zoraxy._endpoint_of("http://docker11.srv:8123/"), "docker11.srv:8123")
    check("https prefix and path", zoraxy._endpoint_of("https://nas.lan:5001/api"), "nas.lan:5001")
    check("uppercase", zoraxy._endpoint_of("DOCKER11.SRV:8123"), "docker11.srv:8123")
    check("without port", zoraxy._endpoint_of("docker11.srv"), None)
    check("garbage", zoraxy._endpoint_of("http://") , None)


def case_topology_from_rules():
    rules = [
        _rule("bgmon.familie-heise.de", ["docker11.srv:5002"]),
        _rule("paperless.familie-heise.de", ["docker12.srv:8000", "docker13.srv:8000"]),
        _rule("api.familie-heise.de", ["docker11.srv:9000"],
              vdirs=[{'Path': '/api', 'Domain': 'docker11.srv:9100'}]),
        _rule("off.familie-heise.de", ["docker11.srv:5002"], disabled=True),
    ]
    opener = FakeOpener(rules)
    metrics = zoraxy._collect(opener, "http://proxy", "csrf", 10, "zoraxy01")
    topo = metrics["__topology__"]

    names = [n for n in topo["nodes"] if n["kind"] == "public-name"]
    check("one node per rule", len(names), 4)
    check("proxy node present", [n["kind"] for n in topo["nodes"]].count("reverse-proxy"), 1)

    proxied = sorted(e["dst"] for e in topo["edges"] if e["kind"] == "proxies")
    check("upstreams become endpoints", proxied, [
        "endpoint:docker11.srv:5002",
        "endpoint:docker11.srv:9000",
        "endpoint:docker11.srv:9100",   # virtual directory
        "endpoint:docker12.srv:8000",
        "endpoint:docker13.srv:8000",
    ])
    check("serves edge per name", sum(1 for e in topo["edges"] if e["kind"] == "serves"), 4)
    check("exposes only active names", sum(1 for e in topo["edges"] if e["kind"] == "exposes"), 3)
    check_true("disabled rule exposes nothing",
               not any(e["kind"] == "exposes" and "off.familie-heise.de" in e["src"] for e in topo["edges"]))
    check("metrics rules_total", metrics["zoraxy_rules_total"], 4)
    check("metrics rules_active", metrics["zoraxy_rules_active"], 3)
    check("metrics origins_active", metrics["zoraxy_origins_active"], 5)


def case_login_scrapes_csrf():
    opener = FakeOpener([])
    err, csrf = zoraxy._login(opener, "http://proxy", "admin", "geheim", 10)
    check("login ok", err, None)
    check("csrf token scraped", csrf, "tok123")
    post = [r for r in opener.requests if "/api/auth/login" in r[0]]
    check("login posted once", len(post), 1)
    header_names = {k.lower(): v for k, v in (post[0][1] or {}).items()}
    check_true("csrf header sent", header_names.get("x-csrf-token") == "tok123",
               str(header_names))


def case_login_without_token_fails_loudly():
    opener = FakeOpener([], login_html="<html>no token here</html>")
    err, csrf = zoraxy._login(opener, "http://proxy", "admin", "geheim", 10)
    check_true("missing token is an error", bool(err), str(err))
    check("no csrf", csrf, "")


def case_schema_contract():
    fields = {f["key"] for f in zoraxy.__schema__["fields"]}
    check_true("schema has base/username/password",
               {"base", "username", "password"} <= fields, str(fields))
    check_true("schema has instance", "instance" in fields, str(fields))


def main():
    case_endpoint_parsing()
    case_topology_from_rules()
    case_login_scrapes_csrf()
    case_login_without_token_fails_loudly()
    case_schema_contract()

    failed = 0
    for name, ok, detail in CHECKS:
        if ok:
            print(f"  PASS  {name}")
        else:
            failed += 1
            print(f"  FAIL  {name}: {detail}")
    print(f"{len(CHECKS) - failed}/{len(CHECKS)} bestanden")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())