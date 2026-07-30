#!/usr/bin/env python3
"""unifi_gateway.py — UniFi Cloud Gateway monitoring via local API."""
import http.cookiejar
import json
import random
import sys
import ssl
import time
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
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
            with opener.open(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            rc = data.get('meta', {}).get('rc', '?')
            msg = data.get('meta', {}).get('msg', data.get('meta', {}).get('description', ''))
            if rc != 'ok':
                err = msg or f'rc={rc}'
                return err
            return None
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                delay = (2 ** attempt) + random.uniform(0, 1)
                time.sleep(delay)
                continue
            raise
    return 'rate limited after retries'


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
        dtype = d.get('type', '')
        metrics[f'device:{name}:state'] = d.get('state', -1)
        metrics[f'device:{name}:type'] = dtype
        num_sta = d.get('num_sta')
        if num_sta is not None:
            metrics[f'device:{name}:clients'] = num_sta
        uptime = d.get('uptime')
        if uptime is not None:
            metrics[f'device:{name}:uptime'] = uptime
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
        body = e.read().decode(errors='replace')[:200]
        print(json.dumps({'error': f'unifi api error {e.code}: {e.reason}', 'detail': body}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({'error': str(e)}))
        sys.exit(1)
