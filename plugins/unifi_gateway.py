#!/usr/bin/env python3
"""unifi_gateway.py — UniFi Cloud Gateway monitoring via local API."""
import http.cookiejar
import json
import sys
import ssl
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
    ],
}


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


def _login(opener, host, username, password):
    url = f'https://{host}/api/auth/login'
    body = json.dumps({'username': username, 'password': password, 'remember': True}).encode()
    req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
    with opener.open(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    if data.get('meta', {}).get('rc') != 'ok':
        return data.get('meta', {}).get('msg', 'login failed')
    return None


def _api_get(opener, host, site, path):
    url = f'https://{host}/proxy/network/api/s/{site}{path}'
    req = urllib.request.Request(url)
    with opener.open(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _collect(opener, host, site):
    metrics = {}

    gw = _api_get(opener, host, site, '/stat/gateway')
    gw_data = gw.get('data', [{}])[0] if gw.get('data') else {}
    if gw_data:
        metrics['gateway_status'] = 'online'
        cpu = gw_data.get('system_stats', {}).get('cpu')
        if cpu is not None:
            metrics['gateway_cpu_pct'] = round(cpu, 1)
        mem = gw_data.get('system_stats', {}).get('mem')
        if mem is not None:
            metrics['gateway_mem_pct'] = round(mem, 1)
        uptime = gw_data.get('uptime')
        if uptime is not None:
            metrics['gateway_uptime'] = uptime
        wan = gw_data.get('wan1')
        if wan:
            rx = wan.get('rx_bytes')
            tx = wan.get('tx_bytes')
            if rx is not None:
                metrics['wan_rx_bytes'] = rx
            if tx is not None:
                metrics['wan_tx_bytes'] = tx
    else:
        metrics['gateway_status'] = 'offline'

    health = _api_get(opener, host, site, '/stat/health')
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

    devices = _api_get(opener, host, site, '/stat/device')
    device_list = devices.get('data', [])
    metrics['devices_total'] = len(device_list)
    metrics['devices_online'] = sum(1 for d in device_list if d.get('state') == 1)
    metrics['devices_offline'] = sum(1 for d in device_list if d.get('state') != 1)
    for d in device_list:
        name = d.get('name', d.get('mac', 'unknown')).replace('.', '_')
        metrics[f'device:{name}:state'] = d.get('state', -1)
        metrics[f'device:{name}:type'] = d.get('type', 'unknown')
        num_sta = d.get('num_sta')
        if num_sta is not None:
            metrics[f'device:{name}:clients'] = num_sta
        uptime = d.get('uptime')
        if uptime is not None:
            metrics[f'device:{name}:uptime'] = uptime

    clients = _api_get(opener, host, site, '/stat/sta')
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

    if not host or not password:
        print(json.dumps({'error': 'api_host and password required'}))
        sys.exit(1)

    opener, _ = _opener(verify)

    err = _login(opener, host, username, password)
    if err:
        print(json.dumps({'error': f'unifi login failed: {err}'}))
        sys.exit(1)

    try:
        metrics = _collect(opener, host, site)
        print(json.dumps(metrics))
    except urllib.error.HTTPError as e:
        print(json.dumps({'error': f'unifi api error {e.code}: {e.reason}'}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({'error': str(e)}))
        sys.exit(1)
