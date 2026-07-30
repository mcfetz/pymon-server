#!/usr/bin/env python3
"""proxmox.py — Proxmox VE cluster monitoring. Uses urllib (stdlib)."""
import json
import sys
import urllib.error
import urllib.request
import ssl

__schema__ = {
    'label': 'Proxmox',
    'description': 'Proxmox VE cluster, VM and LXC monitoring',
    'fields': [
        {'key': 'sleep', 'label': 'Interval (s)', 'type': 'number', 'default': 120, 'min': 30},
        {'key': 'api_host', 'label': 'Proxmox host (hostname:port)', 'type': 'string', 'default': '192.168.1.100:8006'},
        {'key': 'api_token', 'label': 'API Token (USER@REALM!TOKENID=UUID)', 'type': 'string'},
        {'key': 'verify_ssl', 'label': 'Verify SSL', 'type': 'boolean', 'default': False, 'optional': True},
        {'key': 'timeout', 'label': 'Request timeout (s)', 'type': 'number', 'default': 15, 'min': 5, 'optional': True},
    ],
}

def _request(config, path):
    api_host = config['api_host']
    token = config['api_token']
    timeout = config.get('timeout', 15)
    url = f'https://{api_host}/api2/json{path}'

    ctx = ssl.create_default_context()
    if not config.get('verify_ssl', False):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, headers={'Authorization': f'PVEAPIToken={token}'})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            return data.get('data', [])
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError) as e:
        return {'_error': str(e)}


def _name(label):
    name = label.lower().replace(' ', '_').replace('-', '_').replace('.', '_')
    return ''.join(c for c in name if c.isalnum() or c == '_')


if __name__ == '__main__':
    config = json.load(sys.stdin)
    if 'api_host' not in config or 'api_token' not in config:
        print(json.dumps({'error': 'api_host and api_token required'}))
        sys.exit(1)

    metrics = {}

    resources = _request(config, '/cluster/resources')
    if isinstance(resources, dict) and '_error' in resources:
        print(json.dumps({'error': resources['_error']}))
        sys.exit(1)

    vms = [r for r in resources if r.get('type') == 'qemu']
    lxc = [r for r in resources if r.get('type') == 'lxc']
    nodes = [r for r in resources if r.get('type') == 'node']

    metrics['nodes_total'] = len(nodes)
    metrics['nodes_online'] = sum(1 for n in nodes if n.get('status') == 'online')

    metrics['vms_total'] = len(vms)
    metrics['vms_running'] = sum(1 for v in vms if v.get('status') == 'running')
    metrics['vms_stopped'] = sum(1 for v in vms if v.get('status') == 'stopped')

    metrics['lxc_total'] = len(lxc)
    metrics['lxc_running'] = sum(1 for c in lxc if c.get('status') == 'running')
    metrics['lxc_stopped'] = sum(1 for c in lxc if c.get('status') == 'stopped')

    for vm in vms:
        vm_id = vm.get('vmid')
        name = _name(vm.get('name', f'vm_{vm_id}'))
        metrics[f'vm:{name}:status'] = vm.get('status', 'unknown')
        cpu = vm.get('cpu')
        if cpu is not None:
            metrics[f'vm:{name}:cpu'] = round(cpu * 100, 1)
        mem = vm.get('mem')
        maxmem = vm.get('maxmem')
        if mem is not None and maxmem and maxmem > 0:
            metrics[f'vm:{name}:mem_pct'] = round(mem / maxmem * 100, 1)
        disk = vm.get('disk')
        maxdisk = vm.get('maxdisk')
        if disk is not None and maxdisk and maxdisk > 0:
            metrics[f'vm:{name}:disk_pct'] = round(disk / maxdisk * 100, 1)
        uptime = vm.get('uptime')
        if uptime is not None:
            metrics[f'vm:{name}:uptime'] = uptime

    for ct in lxc:
        ct_id = ct.get('vmid')
        name = _name(ct.get('name', f'lxc_{ct_id}'))
        metrics[f'lxc:{name}:status'] = ct.get('status', 'unknown')
        cpu = ct.get('cpu')
        if cpu is not None:
            metrics[f'lxc:{name}:cpu'] = round(cpu * 100, 1)
        mem = ct.get('mem')
        maxmem = ct.get('maxmem')
        if mem is not None and maxmem and maxmem > 0:
            metrics[f'lxc:{name}:mem_pct'] = round(mem / maxmem * 100, 1)
        disk = ct.get('disk')
        maxdisk = ct.get('maxdisk')
        if disk is not None and maxdisk and maxdisk > 0:
            metrics[f'lxc:{name}:disk_pct'] = round(disk / maxdisk * 100, 1)
        uptime = ct.get('uptime')
        if uptime is not None:
            metrics[f'lxc:{name}:uptime'] = uptime

    for node in nodes:
        node_name = _name(node.get('node', 'unknown'))
        metrics[f'node:{node_name}:status'] = node.get('status', 'unknown')
        cpu = node.get('cpu')
        if cpu is not None:
            metrics[f'node:{node_name}:cpu'] = round(cpu * 100, 1)
        mem = node.get('mem')
        maxmem = node.get('maxmem')
        if mem is not None and maxmem and maxmem > 0:
            metrics[f'node:{node_name}:mem_pct'] = round(mem / maxmem * 100, 1)
        disk = node.get('disk')
        maxdisk = node.get('maxdisk')
        if disk is not None and maxdisk and maxdisk > 0:
            metrics[f'node:{node_name}:disk_pct'] = round(disk / maxdisk * 100, 1)
        uptime = node.get('uptime')
        if uptime is not None:
            metrics[f'node:{node_name}:uptime'] = uptime

    print(json.dumps(metrics))
