#!/usr/bin/env python3
"""unifi_gateway.py — UniFi Cloud Gateway monitoring via local API."""
import base64
import http.cookiejar
import json
import random
import sys
import ssl
import time
import urllib.error
import urllib.request

__schema__ = {
    'label': 'UniFi Gateway',
    'description': 'UniFi Cloud Gateway / UniFi OS gateway monitoring',
    'fields': [
        {'key': 'sleep', 'label': 'Interval (s)', 'type': 'number', 'default': 60, 'min': 30},
        {'key': 'api_host', 'label': 'Gateway hostname or IP', 'type': 'string', 'default': '192.168.1.1'},
        {'key': 'username', 'label': 'Local admin username', 'type': 'string', 'default': 'admin'},
        {'key': 'password', 'label': 'Local admin password', 'type': 'string'},
        {'key': 'site', 'label': 'Site name', 'type': 'string', 'default': 'default', 'optional': True},
        {'key': 'verify_ssl', 'label': 'Verify SSL', 'type': 'boolean', 'default': False, 'optional': True},
        {
            'key': 'timeout',
            'label': 'HTTP timeout (s)',
            'type': 'number',
            'default': 25,
            'min': 5,
            'max': 25,
            'optional': True,
        },
    ],
}

# The UniFi OS controller stalls for well over 15 s while it is busy (and the
# old hardcoded 15 s ceiling produced "The read operation timed out" runs). The
# agent kills a plugin after PLUGIN_TIMEOUT (30 s) and reports that as
# agent:error=2, so every request is clamped to what is left of TOTAL_BUDGET:
# a slow controller then yields a readable exit(1) instead of a subprocess kill.
# Muster: plugins/synology_dsm.py.
DEFAULT_TIMEOUT = 25.0
MAX_TIMEOUT = 25.0
TOTAL_BUDGET = 28.0


def _opener(verify):
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        urllib.request.HTTPCookieProcessor(jar),
    )
    return opener, jar


def _resolve_timeout(value):
    """Clamp the configured per-request timeout into [5, MAX_TIMEOUT] seconds."""
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    return max(5.0, min(timeout, MAX_TIMEOUT))


def _slice(want, deadline):
    """Cap a per-request timeout to what is left of the run's time budget."""
    left = deadline - time.monotonic()
    if left <= 1.0:
        raise TimeoutError('plugin time budget exhausted')
    return min(want, left)


def _extract_csrf_from_jar(jar):
    for cookie in jar:
        if cookie.name == 'TOKEN':
            parts = cookie.value.split('.')
            if len(parts) >= 2:
                padded = parts[1] + '=' * (-len(parts[1]) % 4)
                try:
                    payload = json.loads(base64.b64decode(padded))
                    return payload.get('csrfToken')
                except Exception:
                    return None
    return None


def _login(opener, url, username, password, jar, timeout, deadline):
    body = json.dumps({'username': username, 'password': password, 'remember': True}).encode()
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
            with opener.open(req, timeout=_slice(timeout, deadline)) as resp:
                resp.read()
            csrf = _extract_csrf_from_jar(jar)
            if csrf:
                return None, csrf
            return 'no TOKEN cookie found after login', None
        except urllib.error.HTTPError as e:
            if e.code == 429:
                if attempt >= 3:
                    return 'rate limited after retries', None
                delay = (2 ** attempt) + random.uniform(0, 1)
                # Never sleep past the run budget: a rate limit at the end of
                # the budget is reported, not waited out.
                if deadline - time.monotonic() <= delay + 1.0:
                    return 'rate limited after retries', None
                time.sleep(delay)
                continue
            raise
    return 'rate limited after retries', None


def _api_get(opener, url, csrf, timeout, deadline):
    headers = {}
    if csrf:
        headers['X-CSRF-Token'] = csrf
    req = urllib.request.Request(url, headers=headers)
    with opener.open(req, timeout=_slice(timeout, deadline)) as resp:
        return json.loads(resp.read().decode())


def _collect(opener, host, site, csrf, timeout, deadline):
    metrics = {}

    def api(path):
        url = f'https://{host}/proxy/network/api/s/{site}{path}'
        return _api_get(opener, url, csrf, timeout, deadline)

    health = api('/stat/health')
    for h in health.get('data', []):
        subsystem = h.get('subsystem', '')
        status = h.get('status', 'unknown')
        metrics[f'health:{subsystem}:status'] = status
        if subsystem == 'wan':
            metrics['wan_status'] = 'up' if status == 'ok' else 'down'
        elif subsystem == 'lan':
            num_user = h.get('num_user')
            if num_user is not None:
                metrics['clients_lan'] = num_user
        elif subsystem == 'wlan_vap_table':
            num_sta = h.get('num_sta')
            if num_sta is not None:
                metrics['clients_wireless'] = num_sta

    devices = api('/stat/device')
    device_list = devices.get('data', [])
    metrics['devices_total'] = len(device_list)
    metrics['devices_online'] = sum(1 for d in device_list if d.get('state') == 1)
    metrics['devices_offline'] = sum(1 for d in device_list if d.get('state') != 1)

    for d in device_list:
        name = d.get('name', d.get('mac', 'unknown')).replace('.', '_')
        dtype = d.get('type', '')
        metrics[f'device:{name}:state'] = d.get('state', -1)
        metrics[f'device:{name}:type'] = dtype
        num_sta = d.get('num_sta')
        if num_sta is not None:
            metrics[f'device:{name}:clients'] = num_sta
        uptime = d.get('uptime')
        if uptime is not None:
            metrics[f'device:{name}:uptime'] = uptime

        gtypes = ('ugw', 'udm', 'ufg', 'usg')
        if dtype.lower().startswith(gtypes):
            sys_stats = d.get('system_stats', {}) or {}
            cpu = sys_stats.get('cpu')
            if cpu is not None:
                metrics['gateway_cpu_pct'] = round(cpu, 1)
            mem = sys_stats.get('mem')
            if mem is not None:
                metrics['gateway_mem_pct'] = round(mem, 1)
            metrics['gateway_uptime'] = uptime if uptime is not None else -1
            metrics['gateway_status'] = 'online' if d.get('state') == 1 else 'offline'
            wan1 = d.get('wan1')
            if wan1:
                rx = wan1.get('rx_bytes')
                tx = wan1.get('tx_bytes')
                if rx is not None:
                    metrics['wan_rx_bytes'] = rx
                if tx is not None:
                    metrics['wan_tx_bytes'] = tx

        is_switch = any(x in dtype for x in ('usw', 'us-', 'us_', 'sw'))
        is_ap = any(x in dtype for x in ('uap', 'u7', 'uw', 'ap-'))
        if is_switch or 'switch' in dtype.lower():
            sys_stats = d.get('sys_stats', {}) or {}
            mem = sys_stats.get('mem')
            if mem is not None:
                metrics[f'switch:{name}:mem_pct'] = round(mem, 1)
            cpu = sys_stats.get('cpu')
            if cpu is not None:
                metrics[f'switch:{name}:cpu_pct'] = round(cpu, 1)
            total_max_power = d.get('total_max_power')
            if total_max_power is not None:
                metrics[f'switch:{name}:poe_budget'] = total_max_power
            mac_table = d.get('mac_table', [])
            if mac_table is not None:
                metrics[f'switch:{name}:mac_entries'] = len(mac_table)
            for port in d.get('port_table', []):
                port_idx = port.get('port_idx', '?')
                metrics[f'switch:{name}:port:{port_idx}:enable'] = 1 if port.get('enable') else 0
                speed = port.get('speed')
                if speed is not None:
                    metrics[f'switch:{name}:port:{port_idx}:speed'] = speed
                poe_enable = port.get('poe_enable')
                if poe_enable is not None:
                    metrics[f'switch:{name}:port:{port_idx}:poe_enable'] = 1 if poe_enable else 0
                poe_power = port.get('poe_power')
                if poe_power is not None:
                    metrics[f'switch:{name}:port:{port_idx}:poe_power'] = round(poe_power, 3)
        if is_ap:
            for radio in d.get('radio_table', []):
                band = radio.get('radio', 'unknown')
                chan = radio.get('channel')
                if chan is not None:
                    metrics[f'ap:{name}:radio:{band}:channel'] = chan
                cu = radio.get('channel_utilization')
                if cu is not None:
                    metrics[f'ap:{name}:radio:{band}:utilization_pct'] = cu
                tx_power = radio.get('tx_power')
                if tx_power is not None:
                    metrics[f'ap:{name}:radio:{band}:tx_power'] = tx_power
            vap_clients_2g = 0
            vap_clients_5g = 0
            for vap in d.get('vap_table', []):
                if vap.get('band') in ('ng', '2g'):
                    vap_clients_2g += vap.get('num_sta', 0)
                elif vap.get('band') in ('na', '5g', '5ghz'):
                    vap_clients_5g += vap.get('num_sta', 0)
            if vap_clients_2g:
                metrics[f'ap:{name}:clients_2g'] = vap_clients_2g
            if vap_clients_5g:
                metrics[f'ap:{name}:clients_5g'] = vap_clients_5g

    clients = api('/stat/sta')
    sta_list = clients.get('data', [])
    metrics['clients_total'] = len(sta_list)
    metrics['clients_wired'] = sum(1 for c in sta_list if c.get('is_wired', False))

    return metrics


if __name__ == '__main__':
    config = json.load(sys.stdin)
    host = config.get('api_host', '').strip()
    username = config.get('username', 'admin').strip()
    password = config.get('password', '').strip()
    site = config.get('site', 'default').strip()
    verify = config.get('verify_ssl', False)
    timeout = _resolve_timeout(config.get('timeout', DEFAULT_TIMEOUT))

    if not host or not password:
        print(json.dumps({'error': 'api_host and password required'}))
        sys.exit(1)

    deadline = time.monotonic() + TOTAL_BUDGET
    opener, jar = _opener(verify)
    login_url = f'https://{host}/api/auth/login'

    try:
        err, csrf = _login(opener, login_url, username, password, jar, timeout, deadline)
    except Exception as e:
        # Keep the diagnostic on stderr so the agent logs it (stdout carries the
        # machine-readable contract) and always exit(1) instead of crashing with
        # a traceback.
        print(f'unifi: login request failed: {e!r}', file=sys.stderr)
        print(json.dumps({'error': f'unifi login failed: {e}'}))
        sys.exit(1)
    if err:
        print(json.dumps({'error': f'unifi login failed: {err}'}))
        sys.exit(1)

    try:
        metrics = _collect(opener, host, site, csrf, timeout, deadline)
        print(json.dumps(metrics))
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors='replace')[:200]
        print(json.dumps({'error': f'unifi api error {e.code}: {e.reason}', 'detail': body}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({'error': str(e)}))
        sys.exit(1)
