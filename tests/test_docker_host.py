"""Regression tests for the docker_host plugin's swarm-aware state helpers.

Everything is mocked: no Docker daemon and no swarm are required. The core
guarantee is that a container carrying a swarm service label never emits
``container:<name>:running`` — its restart during a rollout is expected and is
tracked at ``service:<name>:up`` instead. The resource stats are covered too:
every running container must get its sample in one poll (the sequential budget
used to cut coverage off after a handful of containers).

Run with:  python tests/test_docker_host.py
"""

import importlib.util
import os
import sys
import time
from contextlib import contextmanager

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLUGIN_PATH = os.path.join(ROOT, "plugins", "docker_host.py")

SWARM_SERVICE_LABEL = "com.docker.swarm.service.id"


def _load_plugin():
    """Import plugins/docker_host.py, stubbing the docker SDK if it is absent.

    The plugin imports the docker SDK at module load, but the pure state
    helpers tested here never touch a daemon, so a stub keeps these tests
    runnable on a machine without the SDK.
    """
    if importlib.util.find_spec("docker") is None:
        import types

        docker_stub = types.ModuleType("docker")
        errors_stub = types.ModuleType("docker.errors")
        errors_stub.APIError = type("APIError", (Exception,), {})
        docker_stub.errors = errors_stub
        sys.modules["docker"] = docker_stub
        sys.modules["docker.errors"] = errors_stub

    spec = importlib.util.spec_from_file_location("docker_host", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


docker_host = _load_plugin()


class FakeContainer:
    """Minimal stand-in for a docker SDK Container."""

    def __init__(
        self, name, status="running", service_id=None, short_id=None, container_id=None
    ):
        self.name = name
        self.short_id = short_id or name
        self.id = container_id or short_id or name
        self.status = status
        labels = {SWARM_SERVICE_LABEL: service_id} if service_id else {}
        self.attrs = {"Config": {"Labels": labels}}


class FakeService:
    """Minimal stand-in for a docker SDK Service."""

    def __init__(self, service_id, name, replicas=None, image=None):
        self.id = service_id
        self.name = name
        mode = (
            {"Replicated": {"Replicas": replicas}}
            if replicas is not None
            else {"Global": {}}
        )
        spec = {"Mode": mode}
        if image is not None:
            spec["TaskTemplate"] = {"ContainerSpec": {"Image": image}}
        self.attrs = {"Spec": spec}


class FakeContainersAPI:
    """Minimal stand-in for client.containers, keyed by health filter value."""

    def __init__(self, by_health):
        self._by_health = by_health

    def list(self, filters=None):
        return self._by_health.get((filters or {}).get("health"), [])


class FakeClient:
    """Minimal stand-in for a docker SDK client exposing api.tasks()."""

    def __init__(self, tasks=(), health=None):
        self.api = self
        self._tasks = list(tasks)
        self.containers = FakeContainersAPI(health or {})

    def tasks(self, filters=None):
        return self._tasks


@contextmanager
def patched(**attrs):
    """Temporarily replace module-level attributes and restore them."""
    saved = {}
    for key, value in attrs.items():
        saved[key] = getattr(docker_host, key)
        setattr(docker_host, key, value)
    try:
        yield
    finally:
        for key, value in saved.items():
            setattr(docker_host, key, value)


# ---------------------------------------------------------------------------
# container state
# ---------------------------------------------------------------------------


def test_running_plain_container_emits_running():
    metrics = {}
    docker_host._container_states([FakeContainer("web")], [], metrics)
    assert metrics == {"container:web:running": 1}


def test_stopped_plain_container_emits_zero():
    metrics = {}
    docker_host._container_states([FakeContainer("web", status="exited")], [], metrics)
    assert metrics == {"container:web:running": 0}


def test_blank_name_falls_back_to_short_id():
    metrics = {}
    docker_host._container_states([FakeContainer("  ", short_id="abc123")], [], metrics)
    assert metrics == {"container:abc123:running": 1}


def test_swarm_task_is_not_reported_as_container_running():
    svc = FakeService("svc1", "app")
    metrics = {}
    docker_host._container_states(
        [FakeContainer("app.1", service_id="svc1")], [svc], metrics
    )
    assert metrics == {"container:app.1:service": "app"}
    assert "container:app.1:running" not in metrics


def test_swarm_task_without_known_service_still_has_no_running():
    # On a worker node services.list() fails, so the service cannot be
    # resolved; the task container must still stay out of the running metric.
    metrics = {}
    docker_host._container_states(
        [FakeContainer("app.1", service_id="svc1")], [], metrics
    )
    assert "container:app.1:running" not in metrics
    assert "container:app.1:service" not in metrics


# ---------------------------------------------------------------------------
# service state
# ---------------------------------------------------------------------------


def test_service_up_when_desired_replicas_run():
    svc = FakeService("svc1", "app", replicas=2)
    tasks = [
        {"ServiceID": "svc1", "Status": {"State": "running"}},
        {"ServiceID": "svc1", "Status": {"State": "running"}},
    ]
    metrics = {}
    docker_host._service_states(FakeClient(tasks), [svc], metrics)
    assert metrics["service:app:replicas"] == 2
    assert metrics["service:app:tasks_running"] == 2
    assert metrics["service:app:up"] == 1


def test_service_down_when_a_task_is_not_running():
    svc = FakeService("svc1", "app", replicas=2)
    tasks = [
        {"ServiceID": "svc1", "Status": {"State": "running"}},
        {"ServiceID": "svc1", "Status": {"State": "pending"}},
    ]
    metrics = {}
    docker_host._service_states(FakeClient(tasks), [svc], metrics)
    assert metrics["service:app:tasks_running"] == 1
    assert metrics["service:app:up"] == 0


def test_global_service_up_with_any_running_task():
    svc = FakeService("svc1", "agent", replicas=None)
    tasks = [{"ServiceID": "svc1", "Status": {"State": "running"}}]
    metrics = {}
    docker_host._service_states(FakeClient(tasks), [svc], metrics)
    assert "service:agent:replicas" not in metrics
    assert metrics["service:agent:up"] == 1


# ---------------------------------------------------------------------------
# image update checks
# ---------------------------------------------------------------------------


def test_update_check_candidates_exclude_swarm_tasks():
    plain = FakeContainer("web", container_id="web1")
    task = FakeContainer("app.1", service_id="svc1", container_id="task1")
    client = FakeClient(health={"healthy": [plain, task], "none": []})
    candidates = docker_host._update_check_candidates(
        client, [plain, task], include_unchecked=False
    )
    assert candidates == [plain]


def test_check_service_reports_newer_image_under_service_namespace():
    svc = FakeService("svc1", "app", image="app:1")
    with patched(
        _resolve_image=lambda client, ref: object(),
        _local_digest=lambda image: "sha256:local",
        _remote_digest=lambda registry, repository, tag, username, password: (
            "sha256:remote"
        ),
    ):
        namespace, name, ref, status = docker_host._check_service(
            {}, FakeClient(), svc, 360, "", ""
        )
    assert namespace == "service"
    assert name == "app"
    assert ref == "app:1"
    assert status == 1


def test_update_checks_emits_stable_service_metric_not_task_metric():
    svc = FakeService("svc1", "app", image="app:1")
    config = {"base_url": "", "check_updates_interval_min": 360}
    with patched(
        _resolve_image=lambda client, ref: object(),
        _local_digest=lambda image: "sha256:local",
        _remote_digest=lambda registry, repository, tag, username, password: (
            "sha256:remote"
        ),
        _load_cache=lambda base_url: {},
        _save_cache=lambda base_url, cache: None,
    ):
        metrics = {}
        docker_host._update_checks(FakeClient(), [], [svc], metrics, config)
    assert metrics["service:app:image_outdated"] == 1
    assert metrics["service:app:image"] == "app:1"
    assert metrics["containers_updates_available"] == 1
    assert not any("app.1" in key for key in metrics)


# ---------------------------------------------------------------------------
# per-container resource stats
# ---------------------------------------------------------------------------


def _stats_payload(
    total_usage=200,
    precpu_total=100,
    system_cpu=1000,
    precpu_system=500,
    online_cpus=2,
    mem_usage=100,
    mem_limit=1000,
    mem_stats=None,
    pids=5,
    read=1,
    write=2,
    rx=3,
    tx=4,
):
    """Docker stats payload; defaults yield cpu_percent 40.0 and 10.0 % memory."""
    memory_stats = {
        "usage": mem_usage,
        "limit": mem_limit,
        "pids_stats": {"current": pids},
    }
    if mem_stats is not None:
        memory_stats["stats"] = mem_stats
    return {
        "cpu_stats": {
            "cpu_usage": {"total_usage": total_usage},
            "system_cpu_usage": system_cpu,
            "online_cpus": online_cpus,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": precpu_total},
            "system_cpu_usage": precpu_system,
            "online_cpus": online_cpus,
        },
        "memory_stats": memory_stats,
        "blkio_stats": {
            "io_service_bytes_recursive": [
                {"op": "read", "value": read},
                {"op": "write", "value": write},
            ]
        },
        "networks": {"eth0": {"rx_bytes": rx, "tx_bytes": tx}},
    }


class FakeStatsContainer(FakeContainer):
    """FakeContainer whose stats() sleeps, then returns a canned payload."""

    def __init__(self, name, payload=None, delay=0.0, error=None, **kwargs):
        super().__init__(name, **kwargs)
        self.payload = payload if payload is not None else _stats_payload()
        self.delay = delay
        self.error = error
        self.stats_calls = 0

    def stats(self, stream=False):
        self.stats_calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.payload


def test_resource_stats_samples_every_running_container_in_one_wave():
    # Regression against the sequential 8s budget, which sampled only the
    # first ~5 containers and left the rest without any metric.
    running = [FakeStatsContainer(f"c{i}", delay=0.5) for i in range(8)]
    metrics = {}
    start = time.monotonic()
    docker_host._resource_stats(running, metrics)
    elapsed = time.monotonic() - start
    for c in running:
        assert f"container:{c.name}:mem_percent" in metrics
        assert f"container:{c.name}:io_write_bytes" in metrics
    assert metrics["containers_stats_collected"] == 8
    assert metrics["containers_stats_missing"] == 0
    assert elapsed < 3.0, f"stats ran sequentially ({elapsed:.2f}s)"


def test_resource_stats_keeps_going_when_one_stats_call_fails():
    running = [
        FakeStatsContainer("a"),
        FakeStatsContainer("b"),
        FakeStatsContainer("broken", error=RuntimeError("daemon went away")),
        FakeStatsContainer("d"),
    ]
    metrics = {}
    docker_host._resource_stats(running, metrics)
    assert "container:a:mem_percent" in metrics
    assert "container:b:mem_percent" in metrics
    assert "container:d:mem_percent" in metrics
    assert not any("broken" in key for key in metrics)
    assert metrics["containers_stats_collected"] == 3
    assert metrics["containers_stats_missing"] == 1


def test_resource_stats_on_empty_list_reports_zero_counters():
    metrics = {}
    docker_host._resource_stats([], metrics)
    assert metrics == {"containers_stats_collected": 0, "containers_stats_missing": 0}


def test_stats_counters_ignore_values_of_broken_container():
    # A stats call raising a non-RuntimeError (e.g. the SDK's APIError) is
    # swallowed as well and only shows up in the missing counter.
    running = [
        FakeStatsContainer("ok1"),
        FakeStatsContainer("ok2", payload=_stats_payload(mem_usage=500)),
        FakeStatsContainer("dead", error=Exception("409 conflict")),
    ]
    metrics = {}
    docker_host._resource_stats(running, metrics)
    assert metrics["containers_stats_collected"] == 2
    assert metrics["containers_stats_missing"] == 1


def test_stats_values_land_under_the_right_container_name():
    # Both containers carry the same suffixes; the merge must not cross them.
    a = FakeStatsContainer("alpha", payload=_stats_payload(mem_usage=100))
    b = FakeStatsContainer("bravo", payload=_stats_payload(mem_usage=500))
    metrics = {}
    docker_host._resource_stats([a, b], metrics)
    assert metrics["container:alpha:mem_percent"] == 10.0
    assert metrics["container:bravo:mem_percent"] == 50.0
    assert metrics["container:alpha:mem_usage_bytes"] == 100
    assert metrics["container:bravo:mem_usage_bytes"] == 500


def test_stats_one_returns_name_and_suffixes_without_touching_metrics():
    values = docker_host._stats_one(FakeStatsContainer("web"))
    name, extracted = values
    assert name == "web"
    assert extracted == {
        "cpu_percent": 40.0,
        "mem_usage_bytes": 100,
        "mem_limit_bytes": 1000,
        "mem_percent": 10.0,
        "pids": 5,
        "io_read_bytes": 1,
        "io_write_bytes": 2,
        "net_rx_bytes": 3,
        "net_tx_bytes": 4,
    }
    broken = FakeStatsContainer("  ", short_id="abc", error=RuntimeError())
    assert docker_host._stats_one(broken) is None


def test_mem_usage_subtracts_the_reclaimable_page_cache():
    # Real numbers from the hermes-agent-andrea container: 3.81 GiB cgroup
    # usage, of which 2.53 GiB was file cache. Docker stats reported 1.28 GiB
    # (its calculation subtracts the inactive file pages); the old metric read
    # the full 3.81 GiB and alarmed at 95 % of a 4 GiB limit although only
    # ~1 GiB was actually held by processes.
    payload = _stats_payload(
        mem_usage=4096778240,
        mem_limit=4294967296,
        mem_stats={"anon": 1123590144, "file": 2720219136, "inactive_file": 2720219136},
    )
    _, values = docker_host._stats_one(FakeStatsContainer("agent", payload=payload))
    assert values["mem_usage_bytes"] == 1376559104
    assert values["mem_percent"] == 32.1
    assert values["mem_usage_bytes"] + 2720219136 == 4096778240


def test_mem_usage_uses_cgroup1_total_inactive_file():
    # cgroup v1 reports 'total_inactive_file' plus a raw 'cache' field that
    # also covers the active, non-reclaimable pages: the inactive figure wins.
    payload = _stats_payload(
        mem_usage=1000,
        mem_limit=4000,
        mem_stats={"cache": 900, "total_inactive_file": 600, "inactive_file": 700},
    )
    _, values = docker_host._stats_one(FakeStatsContainer("c1", payload=payload))
    assert values["mem_usage_bytes"] == 400
    assert values["mem_percent"] == 10.0


def test_mem_usage_without_cache_stats_stays_at_raw_usage():
    payload = _stats_payload(mem_usage=100, mem_limit=1000)
    _, values = docker_host._stats_one(FakeStatsContainer("plain", payload=payload))
    assert values["mem_usage_bytes"] == 100
    assert values["mem_percent"] == 10.0


def test_mem_usage_never_goes_negative_on_an_absurd_cache_value():
    # A stats payload reporting more cache than usage is nonsense; it must be
    # ignored rather than subtracting into a negative (or absurdly low) value.
    payload = _stats_payload(
        mem_usage=100,
        mem_limit=1000,
        mem_stats={"inactive_file": 5000},
    )
    _, values = docker_host._stats_one(FakeStatsContainer("odd", payload=payload))
    assert values["mem_usage_bytes"] == 100
    assert values["mem_percent"] == 10.0


def main():
    """Run every test_* function; pytest is not installed in the venv."""
    tests = [
        (name, value)
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    failed = []
    for name, test in tests:
        try:
            test()
            print(f"  PASS  {name}")
        except Exception as exc:  # noqa: BLE001 - report, do not abort the run
            failed.append(name)
            print(f"  FAIL  {name}: {exc}")
    print()
    print(f"{len(tests) - len(failed)}/{len(tests)} bestanden")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
