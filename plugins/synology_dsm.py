#!/usr/bin/env python3
"""synology_dsm.py — Synology DiskStation monitoring via DSM Web API."""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import ssl

__schema__ = {
    'label': 'Synology DSM',
    'description': 'Synology DiskStation monitoring (DSM 6/7)',
    'fields': [
        {'key': 'sleep', 'label': 'Interval (s)', 'type': 'number', 'default': 120, 'min': 30},
        {'key': 'host', 'label': 'NAS hostname or IP', 'type': 'string', 'default': '192.168.1.200'},
        {'key': 'port', 'label': 'Port (5001=https, 5000=http)', 'type': 'number', 'default': 5001, 'min': 1, 'max': 65535},
        {'key': 'username', 'label': 'DSM admin username', 'type': 'string', 'default': 'admin'},
        {'key': 'password', 'label': 'DSM admin password', 'type': 'string'},
        {'key': 'use_https', 'label': 'Use HTTPS', 'type': 'boolean', 'default': True, 'optional': True},
        {'key': 'verify_ssl', 'label': 'Verify SSL', 'type': 'boolean', 'default': False, 'optional': True},
    ],
}

API_AUTH = 'SYNO.API.Auth'
API_UTIL = 'SYNO.Core.System.Utilization'
API_STOR = 'SYNO.Storage.CGI.Storage'
API_SHARE = 'SYNO.Core.Share'


def _opener(verify):
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    jar = urllib.request.HTTPCookieProcessor()
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        jar,
    )


def _get(opener, url, timeout=15):
    req = urllib.request.Request(url)
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _post(opener, url, data, timeout=15):
    req = urllib.request.Request(url, data=data.encode())
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _api_url(host, port, https, api, version, method, sid=None, **params):
    scheme = 'https' if https else 'http'
    qs = urllib.parse.urlencode({
        'api': api, 'version': version, 'method': method,
        '_sid': sid or '', **params,
    })
    return f'{scheme}://{host}:{port}/webapi/entry.cgi?{qs}'


def _login(opener, host, port, https, username, password):
    scheme = 'https' if https else 'http'
    url = f'{scheme}://{host}:{port}/webapi/auth.cgi'
    data = urllib.parse.urlencode({
        'api': API_AUTH, 'version': '6', 'method': 'login',
        'account': username, 'passwd': password,
        'session': 'NAS', 'format': 'sid',
    })
    resp = _post(opener, url, data)
    if not resp.get('success'):
        msg = resp.get('error', {}).get('code', 'unknown')
        return None, f'login failed (code {msg})'
    return resp['data']['sid'], None


def _collect(opener, host, port, https, sid):
    metrics = {}

    util = _get(opener, _api_url(host, port, https, API_UTIL, 1, 'get', sid))
    if util.get('success'):
        d = util.get('data', {})
        cpu = d.get('cpu', {})
        load = cpu.get('load', {})
        if 'user' in cpu:
            metrics['cpu_user_pct'] = round(cpu['user'], 1)
        if 'system' in cpu:
            metrics['cpu_system_pct'] = round(cpu['system'], 1)
        if 'iowait' in cpu:
            metrics['cpu_iowait_pct'] = round(cpu['iowait'], 1)
        if 'total' in cpu:
            metrics['cpu_total_pct'] = round(cpu['total'], 1)
        if '1min' in load:
            metrics['cpu_load_1min'] = round(load['1min'], 2)
        if '5min' in load:
            metrics['cpu_load_5min'] = round(load['5min'], 2)
        if '15min' in load:
            metrics['cpu_load_15min'] = round(load['15min'], 2)

        mem = d.get('memory', {})
        if 'real_usage' in mem:
            metrics['mem_pct'] = round(mem['real_usage'], 1)
        if 'real_total' in mem:
            metrics['mem_total_bytes'] = mem['real_total']
        if 'real_used' in mem:
            metrics['mem_used_bytes'] = mem['real_used']

        net = d.get('network', {})
        if 'rx' in net:
            metrics['net_rx_bytes'] = net['rx']
        if 'tx' in net:
            metrics['net_tx_bytes'] = net['tx']

    storage = _get(opener, _api_url(host, port, https, API_STOR, 1, 'load_info', sid))
    if storage.get('success'):
        disks = storage.get('data', {}).get('disks', [])
        metrics['disk_total'] = len(disks)
        healthy = sum(1 for d in disks if d.get('status') in ('normal', 'C0'))
        metrics['disk_healthy'] = healthy
        for d in disks:
            disk_id = d.get('disk', '?')
            metrics[f'disk:{disk_id}:status'] = d.get('status', 'unknown')
            temp = d.get('temp')
            if temp is not None:
                metrics[f'disk:{disk_id}:temp_c'] = temp
            model = d.get('model', '')
            if model:
                metrics[f'disk:{disk_id}:model'] = model.replace(' ', '_')

        volumes = storage.get('data', {}).get('volumes', [])
        metrics['volume_total'] = len(volumes)
        for v in volumes:
            vol_id = v.get('id', '?')
            metrics[f'volume:{vol_id}:status'] = v.get('status', 'unknown')
            device = v.get('device', '')
            if device:
                metrics[f'volume:{vol_id}:device'] = device.replace(' ', '_')
            total = v.get('total_size')
            used = v.get('used_size')
            if total and total > 0:
                metrics[f'volume:{vol_id}:total_bytes'] = total
                if used is not None:
                    metrics[f'volume:{vol_id}:used_bytes'] = used
                    metrics[f'volume:{vol_id}:used_pct'] = round(used / total * 100, 1)

        raids = storage.get('data', {}).get('raid', [])
        metrics['raid_total'] = len(raids)
        for r in raids:
            raid_id = r.get('raid', '?')
            metrics[f'raid:{raid_id}:status'] = r.get('status', 'unknown')
            metrics[f'raid:{raid_id}:type'] = r.get('raid_type', '?')

    share = _get(opener, _api_url(host, port, https, API_SHARE, 1, 'list', sid))
    if share.get('success'):
        for s in share.get('data', {}).get('shares', []):
            name = s.get('name', '?')
            total = s.get('total')
            used = s.get('used')
            if total and total > 0:
                metrics[f'share:{name}:total_bytes'] = total
                if used is not None:
                    metrics[f'share:{name}:used_bytes'] = used
                    metrics[f'share:{name}:used_pct'] = round(used / total * 100, 1)

    return metrics


if __name__ == '__main__':
    config = json.load(sys.stdin)
    host = config.get('host', '').strip()
    port = int(config.get('port', 5001))
    https = bool(config.get('use_https', True))
    username = config.get('username', 'admin').strip()
    password = config.get('password', '').strip()
    verify = bool(config.get('verify_ssl', False))

    if not host or not password:
        print(json.dumps({'error': 'host and password required'}))
        sys.exit(1)

    opener = _opener(verify)

    sid, err = _login(opener, host, port, https, username, password)
    if err:
        print(json.dumps({'error': f'synology: {err}'}))
        sys.exit(1)

    try:
        metrics = _collect(opener, host, port, https, sid)
        print(json.dumps(metrics))
    except urllib.error.HTTPError as e:
        print(json.dumps({'error': f'synology api error {e.code}'}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({'error': str(e)}))
        sys.exit(1)
