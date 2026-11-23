#!/usr/bin/env python3
"""test_docker_host_topology.py — Topologie-Teil des docker_host-Plugins.

Eigenstaendiges Skript (kein pytest), wie die uebrigen Tests: druckt
"x/y bestanden" und endet mit 0 oder 1.

Das Plugin importiert das docker-SDK auf Modulebene. Damit der Test auch auf
Rechnern ohne SDK laeuft, wird ein Minimal-Stub in sys.modules gelegt, bevor
das Plugin importiert wird - das Verhalten der getesteten Funktionen haengt
nicht am echten Daemon.

Hintergrund: der erste Entwurf las das Image ueber container.image.tags. Das
ist ein lazy Attribut, das beim Daemon nachfragt und 404 wirft, sobald das
Image weggeraeumt wurde - auf einem Host mit Image-Hygiene der Normalfall. Der
Fehler riss damals die komplette Topologie mit.
"""
import json
import os
import sys
import types

PLUGIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugins")


def _install_docker_stub():
    if "docker" in sys.modules:
        return
    docker = types.ModuleType("docker")

    def from_env(*_a, **_k):
        return None

    class DockerClient:
        def __init__(self, *_a, **_k):
            pass

    docker.from_env = from_env
    docker.DockerClient = DockerClient
    errors = types.ModuleType("docker.errors")

    class APIError(Exception):
        pass

    errors.APIError = APIError
    docker.errors = errors
    sys.modules["docker"] = docker
    sys.modules["docker.errors"] = errors


_install_docker_stub()
sys.path.insert(0, PLUGIN_DIR)
import docker_host  # noqa: E402


class FakeContainer:
    """Ein Container, dessen Image-Beschreibung normal lesbar ist."""

    def __init__(self, name, labels, ports, status="running", image="paperless:latest"):
        self.name = name
        self.labels = labels
        self.status = status
        self.short_id = "abc123def456"
        self.attrs = {
            "NetworkSettings": {"Ports": ports},
            "Config": {"Image": image, "Labels": labels},
        }

    @property
    def image(self):
        raise AssertionError("container.image darf nicht angefasst werden (Daemon-Call!)")


class PrunedImageContainer(FakeContainer):
    """Ein Container, dessen Image nicht mehr existiert - der alte Fehlerfall."""

    @property
    def image(self):
        raise RuntimeError('404 Client Error: No such image: sha256:b28465...')


def test_service_folding_and_endpoints():
    host_ids = ["host:docker11", "host:docker11.srv", "ip:192.168.181.21", "mac:aa:bb:cc:dd:ee:ff"]
    containers = [
        FakeContainer("paperless.1.vobc58", {"com.docker.swarm.service.name": "paperless"},
                      {"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8008"}]}),
        FakeContainer("homer.1.xyz", {}, {"8080/tcp": [{"HostPort": "8089"}]}),
    ]
    topo = docker_host._topology(containers, host_ids, "docker11")

    assert topo["v"] == 2
    assert len(topo["nodes"]) == 3, topo["nodes"]
    assert len(topo["edges"]) == 2

    by_kind = {n["kind"]: n for n in topo["nodes"]}
    host = by_kind["docker-host"]
    assert host["ids"] == host_ids, host["ids"]

    # Der Swarm-Task wird auf seinen Dienst gefaltet: "paperless", nicht die Task-ID.
    service = by_kind["service"]
    assert service["label"] == "paperless"
    # Und der veroeffentlichte Port wird zum endpoint-Identifier in allen Adressformen,
    # denn genau darauf zeigt der Reverse Proxy.
    assert "endpoint:docker11.srv:8008" in service["ids"], service["ids"]
    assert "endpoint:192.168.181.21:8008" in service["ids"], service["ids"]

    assert by_kind["container"]["label"] == "homer.1.xyz"

    edge = [e for e in topo["edges"] if e["dst"] == service["ids"][0]][0]
    assert edge["kind"] == "runs"
    assert edge["src"] == "host:docker11"
    assert edge["attrs"]["ports"] == [8008]


def test_ingress_task_gets_its_ports_from_the_service():
    """Ein Ingress-Task hat keine eigene Host-Bindung - der Service kennt den Port.

    Ohne diese Quelle sieht jeder ingress-veroeffentlichte Dienst unveroeffentlicht
    aus, und die Proxy-Kante kann ihn nie erreichen. Befund aus dem Betrieb: nur
    3 von 45 Docker-Knoten hatten ueberhaupt Endpunkt-Identifier.
    """
    host_ids = ["host:docker11.srv", "ip:192.168.181.21"]
    containers = [
        FakeContainer("bgmon_bgmon-app.1.abc", {
            "com.docker.swarm.service.name": "bgmon_bgmon-app",
            "com.docker.swarm.service.id": "svc-bgmon",
        }, {}),          # Ingress-Modus: NetworkSettings.Ports ist leer
    ]
    topo = docker_host._topology(containers, host_ids, "docker11", {"svc-bgmon": [5002]})

    service = [n for n in topo["nodes"] if n["kind"] == "service"][0]
    assert service["label"] == "bgmon_bgmon-app", service
    assert "endpoint:docker11.srv:5002" in service["ids"], service["ids"]
    assert "endpoint:192.168.181.21:5002" in service["ids"], service["ids"]
    assert service["attrs"]["ports"] == [5002], service["attrs"]


def test_own_ports_survive_without_the_service_map():
    """Auf einem Worker ist die Service-Karte leer - die eigenen Ports bleiben."""
    host_ids = ["host:docker11.srv"]
    containers = [
        FakeContainer("homer.1.xyz", {}, {"8080/tcp": [{"HostPort": "8089"}]}),
    ]
    topo = docker_host._topology(containers, host_ids, "docker11", {})

    node = [n for n in topo["nodes"] if n["kind"] == "container"][0]
    assert node["attrs"]["ports"] == [8089], node["attrs"]
    assert "endpoint:docker11.srv:8089" in node["ids"], node["ids"]


class _FakeService:
    def __init__(self, service_id, ports):
        self.id = service_id
        self.attrs = {"Endpoint": {"Ports": ports}}


class _FakeSwarm:
    """Ein Manager: listet Services, wie die SDK es tut."""

    def __init__(self, services):
        self.services = self
        self._services = services

    def list(self):
        return self._services


class _WorkerSwarm:
    """Ein Worker: die Service-API wirft - darf die Topologie nicht killen."""

    def list(self):
        raise RuntimeError("This node is not a swarm manager")


def test_swarm_service_ports_are_read_and_failures_are_swallowed():
    ports = docker_host._swarm_service_ports(_FakeSwarm([
        _FakeService("svc-bgmon", [
            {"TargetPort": 5000, "PublishedPort": 5002, "PublishMode": "ingress"},
        ]),
        _FakeService("svc-intern", []),
    ]))
    assert ports == {"svc-bgmon": [5002]}, ports

    assert docker_host._swarm_service_ports(_WorkerSwarm()) == {}


def test_pruned_image_does_not_break_the_graph():
    """Der Fehlerfall aus dem Betrieb: Image weggeraeumt, Topologie muss stehen."""
    host_ids = ["host:docker12"]
    containers = [
        PrunedImageContainer("ntfy.1.abc", {}, {"80/tcp": [{"HostPort": "60000"}]}),
        FakeContainer("grafana.1.def", {"com.docker.swarm.service.name": "grafana"}, {}),
    ]
    topo = docker_host._topology(containers, host_ids, "docker12")

    # Beide Container sind da, keiner wurde uebersprungen: es wird kein
    # Daemon-Call mehr gemacht, also kann auch keiner fehlschlagen.
    assert len(topo["nodes"]) == 3, topo["nodes"]
    assert topo["nodes"][0]["attrs"]["containers_skipped"] == 0


def test_missing_ports_are_harmless():
    host_ids = ["host:docker13"]
    topo = docker_host._topology([FakeContainer("solo", {}, {})], host_ids, "docker13")
    ids = topo["nodes"][1]["ids"]
    assert not [i for i in ids if i.startswith("endpoint:")], ids
    assert topo["edges"][0]["label"] == ""


def test_host_identifiers_are_generic():
    ids = docker_host._host_identifiers()
    assert ids, "der Host muss sich selbst identifizieren koennen"
    assert all(i.split(":", 1)[0] in ("host", "ip", "mac") for i in ids), ids
    # Der Kurzname steht vorn: ueber ihn trifft der Hypervisor den Gast.
    short = os.uname().nodename.split(".")[0].lower()
    assert ids[0] == f"host:{short}", ids


if __name__ == "__main__":
    setup = None
    names = sorted(n for n in dir() if n.startswith("test_"))
    failed = []
    for name in names:
        try:
            globals()[name]()
        except Exception as exc:
            failed.append(name)
            print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"  PASS {name}")
    print(f"{len(names) - len(failed)}/{len(names)} bestanden")
    sys.exit(1 if failed else 0)
