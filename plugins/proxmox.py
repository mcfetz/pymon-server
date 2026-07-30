#!/usr/bin/env python3
"""proxmox.py — Proxmox VE monitoring. Uses pvesh CLI if available, else HTTP API."""
import json
import subprocess
import sys

__schema__ = {
    'label': 'Proxmox',
    'description': 'Proxmox VE cluster, VM and LXC monitoring',
    'fields': [
        {'key': 'sleep', 'label': 'Interval (s)', 'type': 'number', 'default': 120, 'min': 30},
        {'key': 'api_host', 'label': 'API host (only for remote mode)', 'type': 'string', 'default': '', 'optional': True},
        {'key': 'api_token', 'label': 'API Token (only for remote mode)', 'type': 'string', 'default': '', 'optional': True},
        {'key': 'verify_ssl', 'label': 'Verify SSL', 'type': 'boolean', 'default': False, 'optional': True},
    ],
}


def _name(label):
    name = label.lower().replace(' ', '_').replace('-', '_').replace('.', '_')
    return ''.join(c for c in name if c.isalnum() or c == '_')


def _pvesh(path):
    try:
        r = subprocess.run(
            ['pvesh', 'get', path, '--output-format', 'json'],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return {'_error': r.stderr.strip() or f'pvesh exited {r.returncode}'}
        return json.loads(r.stdout)
    except FileNotFoundError:
        return {'_error': 'pvesh not found'}
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        return {'_error': str(e)}


def _http_get(config, path):
    import urllib.error
    import urllib.request
    import ssl

    api_host = config.get('api_host', '').strip()
    token = config.get('api_token', '').strip()
    if not api_host or not token:
        return {'_error': 'api_host and api_token required for remote mode'}
    url = f'https://{api_host}/api2/json{path}'
    ctx = ssl.create_default_context()
    if not config.get('verify_ssl', False):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={'Authorization': f'PVEAPIToken={token}'})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            return data.get('data', [])
    except Exception as e:
        return {'_error': str(e)}


def _resources(config):
    if config.get('api_host') or config.get('api_token'):
        raw = _http_get(config, '/cluster/resources')
    else:
        raw = _pvesh('/cluster/resources')
    if isinstance(raw, dict) and '_error' in raw:
        return None, raw['_error']
    return raw, None


if __name__ == '__main__':
    config = json.load(sys.stdin)

    resources, err = _resources(config)
    if err:
        print(json.dumps({'error': err}))
        sys.exit(1)

    vms = [r for r in resources if r.get('type') == 'qemu']
    lxc = [r for r in resources if r.get('type') == 'lxc']
    nodes = [r for r in resources if r.get('type') == 'node']

    metrics = {
        'nodes_total': len(nodes),
        'nodes_online': sum(1 for n in nodes if n.get('status') == 'online'),
        'vms_total': len(vms),
        'vms_running': sum(1 for v in vms if v.get('status') == 'running'),
        'vms_stopped': sum(1 for v in vms if v.get('status') == 'stopped'),
        'lxc_total': len(lxc),
        'lxc_running': sum(1 for c in lxc if c.get('status') == 'running'),
        'lxc_stopped': sum(1 for c in lxc if c.get('status') == 'stopped'),
    }

    for kind, items in [('vm', vms), ('lxc', lxc), ('node', nodes)]:
        for item in items:
            label = _name(item.get('name') or item.get('node', f'{kind}_{item.get("vmid", "?")}'))
            metrics[f'{kind}:{label}:status'] = item.get('status', 'unknown')
            cpu = item.get('cpu')
            if cpu is not None:
                metrics[f'{kind}:{label}:cpu'] = round(cpu * 100, 1)
            mem = item.get('mem')
            maxmem = item.get('maxmem')
            if mem is not None and maxmem and maxmem > 0:
                metrics[f'{kind}:{label}:mem_pct'] = round(mem / maxmem * 100, 1)
            disk = item.get('disk')
            maxdisk = item.get('maxdisk')
            if disk is not None and maxdisk and maxdisk > 0:
                metrics[f'{kind}:{label}:disk_pct'] = round(disk / maxdisk * 100, 1)
            uptime = item.get('uptime')
            if uptime is not None:
                metrics[f'{kind}:{label}:uptime'] = uptime

    print(json.dumps(metrics))
