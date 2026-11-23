#!/usr/bin/env python3
"""test_proxmox_topology.py — Knoten-Identifier des proxmox-Plugins.

Eigenstaendiges Skript (kein pytest), wie die uebrigen Tests: druckt
"x/y bestanden" und endet mit 0 oder 1.

Hintergrund: Proxmox kennt seine Knoten und Gaeste am Kurznamen ("pve3"), UniFi
und Zoraxy melden den FQDN ("pve3.srv"). Traegt der Proxmox-Knoten den Alias
nicht, steht dieselbe Maschine zweimal im Graphen. Frueher bekam nur der Knoten
den FQDN, auf dem das Plugin selbst laeuft - deshalb verschmolz pve1 und pve3
nicht. Der Resolver wird hier gefakt.
"""
import importlib.util
import os
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("proxmox", os.path.join(HERE, "..", "plugins", "proxmox.py"))
proxmox = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxmox)

CHECKS = []
REAL = (socket.gethostbyname, socket.gethostbyaddr)


def check(name, got, want):
    CHECKS.append((name, got == want, f"{got!r} != {want!r}" if got != want else ""))


def check_true(name, cond, detail=""):
    CHECKS.append((name, bool(cond), "" if cond else detail))


def _resolver(mapping):
    def gethostbyname(name):
        entry = mapping.get(str(name).lower())
        if not entry:
            raise socket.gaierror(8, "nodename nor servname provided, or not known")
        return entry["addr"]

    def gethostbyaddr(addr):
        for entry in mapping.values():
            if entry["addr"] == addr:
                return (entry["primary"], list(entry.get("aliases") or []), [addr])
        raise socket.herror(1, "Unknown host")

    return gethostbyname, gethostbyaddr


def with_resolver(mapping, fn):
    socket.gethostbyname, socket.gethostbyaddr = _resolver(mapping)
    try:
        return fn()
    finally:
        socket.gethostbyname, socket.gethostbyaddr = REAL


PVE3 = {
    "pve3": {
        "addr": "192.168.181.30",
        "primary": "pve3.familie-heise.de",
        "aliases": ["pve3.srv", "pve3"],
    }
}


def case_all_aliases_of_a_node():
    ids = with_resolver(PVE3, lambda: proxmox._host_ids("pve3"))
    check("Kurzname zuerst", ids[0], "host:pve3")
    check_true("srv-Alias dabei", "host:pve3.srv" in ids, str(ids))
    check_true("FQDN dabei", "host:pve3.familie-heise.de" in ids, str(ids))
    check_true("Adresse dabei", "ip:192.168.181.30" in ids, str(ids))
    check_true("keine Dubletten", len(ids) == len(set(ids)), str(ids))


def case_short_name_match_is_the_point():
    """Genau der Fall aus dem Graphen: UniFi/Zoraxy sagen pve3.srv, Proxmox pve3."""
    ids = with_resolver(PVE3, lambda: proxmox._host_ids("pve3"))
    unifi_side = {"host:pve3.srv", "ip:192.168.177.30"}
    check_true("gemeinsamer Identifier vorhanden",
               bool(set(ids) & unifi_side), str(ids))


def case_case_and_space_insensitive():
    ids = with_resolver(PVE3, lambda: proxmox._host_ids("  PVE3  "))
    check("gross und mit Leerzeichen", ids[0], "host:pve3")
    check_true("Alias trotzdem dabei", "host:pve3.srv" in ids, str(ids))


def case_unresolvable_keeps_short_name():
    ids = with_resolver({}, lambda: proxmox._host_ids("gibtsnicht"))
    check("nur Kurzname", ids, ["host:gibtsnicht"])


def case_empty_input():
    check("leer", proxmox._host_ids(""), [])
    check("None", proxmox._host_ids(None), [])
    check("nur Leerzeichen", proxmox._host_ids("   "), [])


def case_guest_gets_alias_too():
    mapping = {
        "docker11": {
            "addr": "192.168.181.21",
            "primary": "docker11.familie-heise.de",
            "aliases": ["docker11.srv"],
        }
    }
    ids = with_resolver(mapping, lambda: proxmox._host_ids("docker11"))
    check_true("Gast-Alias zuerst nach Kurzname", ids[1] == "host:docker11.familie-heise.de", str(ids))
    check_true("Gast-srv-Alias dabei", "host:docker11.srv" in ids, str(ids))


def main():
    case_all_aliases_of_a_node()
    case_short_name_match_is_the_point()
    case_case_and_space_insensitive()
    case_unresolvable_keeps_short_name()
    case_empty_input()
    case_guest_gets_alias_too()

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