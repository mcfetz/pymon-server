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


def _host_ids(host):
    """Every identifier a host answers to: short name, FQDN, DNS aliases, addresses.

    Proxmox knows its nodes and guests by short name ("pve3"), while UniFi and
    Zoraxy report the FQDN ("pve3.srv"). Without the alias the same machine ends
    up as two nodes in the graph, which is exactly what happened: pve1 merged
    because the plugin runs there, pve2 and pve3 did not.

    Unresolvable names are fine - the short name alone still works.
    """
    import socket as _socket

    name = str(host or '').strip().lower()
    if not name:
        return []
    ids = [f'host:{name}']
    try:
        primary, aliases, addrs = _socket.gethostbyaddr(_socket.gethostbyname(name))
    except OSError:
        return ids
    for alias in [primary, *(aliases or [])]:
        alias = str(alias or '').strip().lower()
        if alias and alias != name and f'host:{alias}' not in ids:
            ids.append(f'host:{alias}')
    for addr in addrs or []:
        if f'ip:{addr}' not in ids:
            ids.append(f'ip:{addr}')
    return ids


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

    # ── Topologie: Wirt -> Gast ──
    # Identifier sind absichtlich generisch, damit andere Plugins anschliessen
    # koennen: der Knoten ueber seinen Namen, der Gast ueber den Namen aus der
    # Proxmox-Konfiguration. Zusaetzlich bekommt jeder Gast eine abstrakte ID,
    # damit auch ein namenloser oder gestoppter Gast im Graphen bleibt.
    #
    # Der Kurzname ist bewusst dabei: die Proxmox-Konfiguration kennt Gaeste
    # meist ohne Domain ("docker11"), waehrend der Gast selbst sich als FQDN
    # meldet ("docker11.srv"). Genau ueber den Kurznamen treffen sich beide.
    topo_nodes = []
    topo_edges = []

    for item in nodes:
        node_name = (item.get('node') or item.get('name') or '').strip()
        if not node_name:
            continue
        ids = _host_ids(node_name)
        topo_nodes.append({
            'ids': ids,
            'kind': 'pve-node',
            'label': node_name,
            'attrs': {'status': item.get('status'), 'cluster': item.get('cluster')},
        })

    for item in vms + lxc:
        vmid = item.get('vmid')
        node_name = (item.get('node') or '').strip()
        guest_name = (item.get('name') or '').strip()
        guest_type = 'qemu' if item.get('type') == 'qemu' else 'lxc'
        type_label = 'VM' if guest_type == 'qemu' else 'LXC'

        ids = []
        if guest_name:
            ids = _host_ids(guest_name)
        abstract_id = f'abstract:proxmox:{node_name.lower() or "?"}/{vmid}'
        if abstract_id not in ids:
            ids.append(abstract_id)
        topo_nodes.append({
            'ids': ids,
            'kind': 'pve-guest',
            'label': guest_name or f'{type_label} {vmid}',
            'attrs': {'vmid': vmid, 'type': guest_type, 'status': item.get('status'), 'node': node_name},
        })

        if node_name:
            topo_edges.append({
                'src': f'host:{node_name.lower()}',
                'dst': ids[0],
                'kind': 'guest',
                'label': f'{type_label} {vmid}',
                'attrs': {'vmid': vmid, 'type': guest_type, 'status': item.get('status')},
            })

    if topo_nodes:
        metrics['__topology__'] = {'v': 2, 'nodes': topo_nodes, 'edges': topo_edges}

    print(json.dumps(metrics))
