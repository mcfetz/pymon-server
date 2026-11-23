#!/usr/bin/env python3
"""zoraxy.py — Zoraxy reverse proxy: rules, upstreams and the topology they form.

Pure-ish monitoring plugin: it reports a few counters and, more importantly, the
graph that the proxy rules describe. Each rule is a public name pointing at one
or more upstreams; the upstreams are written as "host:port", which is exactly
the endpoint identifier the docker plugins report for a published port. So the
chain closes without either plugin knowing the other:

    abstract:internet <- host:<public name> -> endpoint:<upstream host:port>

Login follows the admin API: scrape the CSRF token from the login page, post
credentials with that header, then reuse the session cookie.
"""
import http.cookiejar
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

__schema__ = {
    'label': 'Zoraxy',
    'description': 'Zoraxy reverse proxy: rules, upstreams and topology',
    'fields': [
        {'key': 'sleep', 'label': 'Interval (s)', 'type': 'number', 'default': 120, 'min': 30},
        {'key': 'base', 'label': 'Admin base URL (e.g. http://zoraxy01.dmz:8000)', 'type': 'string',
         'default': 'http://zoraxy01.dmz:8000'},
        {'key': 'username', 'label': 'Username', 'type': 'string', 'default': 'admin'},
        {'key': 'password', 'label': 'Password', 'type': 'string', 'default': ''},
        {'key': 'instance', 'label': 'Instance name for the graph node', 'type': 'string',
         'default': 'zoraxy01', 'optional': True},
        {'key': 'verify_ssl', 'label': 'Verify SSL', 'type': 'boolean', 'default': False, 'optional': True},
        {'key': 'timeout', 'label': 'Request timeout (s)', 'type': 'number', 'default': 15, 'min': 3, 'max': 60},
    ],
}

CSRF_META = re.compile(r'<meta\s+name="zoraxy\.csrf\.Token"\s+content="([^"]+)"', re.IGNORECASE)
DEFAULT_TIMEOUT = 15


def _opener(verify):
    jar = http.cookiejar.CookieJar()
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        urllib.request.HTTPCookieProcessor(jar),
    ), jar


def _get(opener, url, timeout, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with opener.open(req, timeout=timeout) as resp:
        return resp.read().decode('utf-8', 'replace')


def _post(opener, url, data, timeout, csrf=None):
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    if csrf:
        headers['X-CSRF-Token'] = csrf
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=headers)
    with opener.open(req, timeout=timeout) as resp:
        return resp.read().decode('utf-8', 'replace')


def _login(opener, base, username, password, timeout):
    """Scrape the CSRF token from the login page, then authenticate."""
    try:
        html = _get(opener, base + '/login.html', timeout)
    except urllib.error.HTTPError:
        # Some builds answer /login.html only after the 307 from /.
        html = _get(opener, base + '/', timeout)
    match = CSRF_META.search(html)
    csrf = match.group(1) if match else ''
    if not csrf:
        return 'no CSRF token on the login page', ''
    try:
        _post(opener, base + '/api/auth/login',
              {'username': username, 'password': password}, timeout, csrf)
    except urllib.error.HTTPError as e:
        return f'login failed: HTTP {e.code}', csrf
    check = _get(opener, base + '/api/auth/checkLogin', timeout)
    if '"error"' in check.lower() or 'true' not in check.lower():
        return 'login rejected', csrf
    return None, csrf


def _endpoint_of(origin):
    """'docker11.srv:8123' or 'http://docker11.srv:8123' -> 'docker11.srv:8123'."""
    text = str(origin or '').strip().lower()
    if not text:
        return None
    for prefix in ('http://', 'https://'):
        if text.startswith(prefix):
            text = text[len(prefix):]
    text = text.split('/', 1)[0]
    if ':' not in text:
        return None
    host, _, port = text.rpartition(':')
    if not host or not port.isdigit():
        return None
    return f'{host}:{int(port)}'


def _collect(opener, base, csrf, timeout, instance):
    metrics = {}
    raw = _get(opener, base + '/api/proxy/list?type=host', timeout)
    rules = json.loads(raw)
    if not isinstance(rules, list):
        raise ValueError('unexpected rule list answer')

    nodes = []
    edges = []
    node_ids = [f'host:{instance.lower()}']
    rules_active = 0
    origins_total = 0
    nodes.append({
        'ids': node_ids,
        'kind': 'reverse-proxy',
        'label': instance,
        'attrs': {'rules': len(rules)},
    })

    for rule in rules:
        if not isinstance(rule, dict):
            continue
        name = str(rule.get('RootOrMatchingDomain') or '').strip().lower()
        if not name:
            continue
        disabled = bool(rule.get('Disabled'))
        active_origins = rule.get('ActiveOrigins') or []
        if active_origins and not disabled:
            rules_active += 1

        name_id = f'host:{name}'
        nodes.append({
            'ids': [name_id],
            'kind': 'public-name',
            'label': name,
            'attrs': {'disabled': disabled,
                      'proxy_auth': (rule.get('AuthenticationProvider') or {}).get('AuthMethod'),
                      'origins': len(active_origins)},
        })
        # The proxy instance serves this name.
        edges.append({
            'src': node_ids[0],
            'dst': name_id,
            'kind': 'serves',
            'attrs': {'disabled': disabled},
        })
        # A disabled rule is not reachable from outside, so it gets no hop to the
        # internet - the name node stays, the exposure does not.
        if not disabled:
            edges.append({
                'src': name_id,
                'dst': 'abstract:internet',
                'kind': 'exposes',
            })
        if disabled or not active_origins:
            continue

        for origin in active_origins:
            if not isinstance(origin, dict):
                continue
            target = _endpoint_of(origin.get('OriginIpOrDomain'))
            if not target:
                continue
            host_part, _, port = target.rpartition(':')
            origins_total += 1
            # The target is the endpoint (host and port), not the host alone: a
            # docker service publishing that port carries this identifier, so the
            # edge lands on the service - the hop that matters. The server folds
            # it onto the host when nobody claims the port, which keeps the NAS
            # ports from becoming one node each.
            edges.append({
                'src': name_id,
                'dst': f'endpoint:{target}',
                'kind': 'proxies',
                'label': target,
                'attrs': {'port': int(port), 'tls': origin.get('RequireTLS'),
                          'weight': origin.get('Weight')},
            })

        # Path routing: a virtual directory sends a prefix to its own backend.
        for vdir in rule.get('VirtualDirectories') or []:
            if not isinstance(vdir, dict):
                continue
            target = _endpoint_of(vdir.get('Domain'))
            path = vdir.get('Path') or ''
            if not target or not path:
                continue
            host_part, _, port = target.rpartition(':')
            origins_total += 1
            edges.append({
                'src': name_id,
                'dst': f'endpoint:{target}',
                'kind': 'proxies',
                'label': f'{path} -> {target}',
                'attrs': {'path': path, 'port': int(port)},
            })

    # Every active public name is reachable from the internet: that is the point
    # of a reverse proxy. "abstract:internet" is the one identifier nobody else
    # can know, which is exactly why it is allowed to be private. The edges are
    # added per rule above, so a disabled rule contributes none.

    metrics['zoraxy_rules_total'] = len(rules)
    metrics['zoraxy_rules_active'] = rules_active
    metrics['zoraxy_origins_active'] = origins_total
    metrics['__topology__'] = {'v': 2, 'nodes': nodes, 'edges': edges}
    return metrics


if __name__ == '__main__':
    config = json.load(sys.stdin)
    base = str(config.get('base') or config.get('api_host') or '').strip().rstrip('/')
    username = str(config.get('username') or 'admin').strip()
    password = str(config.get('password') or '')
    instance = str(config.get('instance') or 'zoraxy').strip()
    verify = bool(config.get('verify_ssl', False))
    try:
        timeout = max(3, min(60, int(config.get('timeout') or DEFAULT_TIMEOUT)))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT

    if not base or not password:
        print(json.dumps({'error': 'base and password required'}))
        sys.exit(1)
    if not base.startswith(('http://', 'https://')):
        base = 'https://' + base

    start = time.monotonic()
    opener, _jar = _opener(verify)
    try:
        err, csrf = _login(opener, base, username, password, timeout)
    except Exception as exc:
        print(f'zoraxy: login request failed: {exc!r}', file=sys.stderr)
        print(json.dumps({'error': f'zoraxy login failed: {exc}'}))
        sys.exit(1)
    if err:
        print(json.dumps({'error': f'zoraxy login failed: {err}'}))
        sys.exit(1)

    try:
        metrics = _collect(opener, base, csrf, timeout, instance)
        metrics['zoraxy_scrape_duration_seconds'] = round(time.monotonic() - start, 3)
        print(json.dumps(metrics))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors='replace')[:200] if hasattr(exc, 'read') else ''
        print(json.dumps({'error': f'zoraxy api error {exc.code}: {exc.reason}', 'detail': detail}))
        sys.exit(1)
    except Exception as exc:
        print(json.dumps({'error': str(exc)}))
        sys.exit(1)