#!/usr/bin/env python3
"""test_unifi_topology_edges.py — welche Kabel-Kanten das unifi-Plugin zieht.

Eigenstaendiges Skript (kein pytest), wie die uebrigen Tests: "x/y bestanden".

Hintergrund: UniFi lernt die MAC eines Gasts auf dem physischen Port seines
Wirts (der Gast geht durch dessen Kabel) und meldet sie als verdrahteten Client.
Daraus entstand frueher eine Kante Gast -> Switch, obwohl das Kabel dem Wirt
gehoert. Virtuelle MACs bekommen deshalb keine Kabel-Kante; der Knoten bleibt.
"""
import importlib.util
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("uni", os.path.join(HERE, "..", "plugins", "unifi_gateway.py"))
uni = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uni)

CHECKS = []

SWITCH_MAC = "9c:05:d6:a2:30:b6"
VM_MAC = "bc:24:11:59:c3:d6"          # Proxmox VE
KVM_MAC = "52:54:00:11:22:33"         # QEMU/KVM
PRINTER_MAC = "aa:bb:cc:dd:ee:01"     # ein echtes Geraet


def check(name, got, want):
    CHECKS.append((name, got == want, f"{got!r} != {want!r}" if got != want else ""))


def check_true(name, cond, detail=""):
    CHECKS.append((name, bool(cond), "" if cond else detail))


class Response:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeController:
    """Answers each endpoint with its own payload - the plugin polls three."""

    def __init__(self, by_path):
        self.by_path = by_path
        self.urls = []

    def open(self, req, timeout=None):
        self.urls.append(req.full_url)
        for needle, payload in self.by_path.items():
            if needle in req.full_url:
                return Response(payload)
        return Response({"data": []})


def collect(devices=None, clients=None):
    opener = FakeController({
        "/stat/health": {"data": []},
        "/stat/device": {"data": devices or []},
        "/stat/sta": {"data": clients or []},
    })
    return uni._collect(opener, "gw.invalid", "default", "csrf", 25.0, time.monotonic() + 60)


def _switched(mac, port):
    return {"mac": mac, "is_wired": True, "sw_mac": SWITCH_MAC, "sw_port": port,
            "hostname": mac.replace(":", "-")}


def _topology(devices, clients):
    metrics = collect(devices=devices, clients=clients)
    return metrics.get("__topology__") or {"nodes": [], "edges": []}


def case_virtual_mac_gets_no_cable_edge():
    topo = _topology(
        [{"mac": SWITCH_MAC, "type": "usw", "name": "us48", "state": 1}],
        [_switched(VM_MAC, 10), _switched(KVM_MAC, 11), _switched(PRINTER_MAC, 7)],
    )
    client_ids = [n["ids"][0] for n in topo["nodes"] if n["kind"] == "client"]
    for label, mac in (("Proxmox", VM_MAC), ("KVM", KVM_MAC), ("Drucker", PRINTER_MAC)):
        check_true(f"{label}-Knoten bleibt im Graphen", f"mac:{mac}" in client_ids, str(client_ids))

    wired = [(e["src"], e["dst"]) for e in topo["edges"] if e["kind"] == "wired"]
    check_true("Proxmox-Gast ohne Kabel-Kante",
               (f"mac:{SWITCH_MAC}", f"mac:{VM_MAC}") not in wired, str(wired))
    check_true("KVM-Gast ohne Kabel-Kante",
               (f"mac:{SWITCH_MAC}", f"mac:{KVM_MAC}") not in wired, str(wired))
    check_true("echtes Geraet behaelt seine Kabel-Kante",
               (f"mac:{SWITCH_MAC}", f"mac:{PRINTER_MAC}") in wired, str(wired))
    check("genau eine Kabel-Kante", len(wired), 1)


def case_wireless_client_keeps_its_edge():
    """Die Regel gilt nur fuer Kabel: ein Gast per WLAN bleibt am AP haengen."""
    topo = _topology(
        [{"mac": "aa:aa:aa:aa:aa:aa", "type": "uap", "name": "og-ap", "state": 1}],
        [{"mac": VM_MAC, "is_wired": False, "ap_mac": "aa:aa:aa:aa:aa:aa",
          "essid": "heim", "hostname": "docker11.srv"}],
    )
    wireless = [(e["src"], e["dst"]) for e in topo["edges"] if e["kind"] == "wireless"]
    check("Funk-Kante bleibt", wireless, [("mac:aa:aa:aa:aa:aa:aa", f"mac:{VM_MAC}")])


def case_switch_port_survives_for_real_clients():
    """Die Portangabe an der Kante bleibt erhalten."""
    topo = _topology(
        [{"mac": SWITCH_MAC, "type": "usw", "name": "us48", "state": 1}],
        [_switched(PRINTER_MAC, 7)],
    )
    wired = [e for e in topo["edges"] if e["kind"] == "wired"]
    check("eine Kante", len(wired), 1)
    check("Port am Attribut", (wired[0].get("attrs") or {}).get("port"), 7)
    check("Port im Label", wired[0].get("label"), "Port 7")


def main():
    case_virtual_mac_gets_no_cable_edge()
    case_wireless_client_keeps_its_edge()
    case_switch_port_survives_for_real_clients()

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