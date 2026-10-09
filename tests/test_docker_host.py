"""Regression tests for the docker_host plugin's swarm-aware state helpers.

Everything is mocked: no Docker daemon and no swarm are required. The core
guarantee is that a container carrying a swarm service label never emits
``container:<name>:running`` — its restart during a rollout is expected and is
tracked at ``service:<name>:up`` instead.

Run with:  python tests/test_docker_host.py
"""

import importlib.util
import os
import sys

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

    def __init__(self, name, status="running", service_id=None, short_id=None):
        self.name = name
        self.short_id = short_id or name
        self.status = status
        labels = {SWARM_SERVICE_LABEL: service_id} if service_id else {}
        self.attrs = {"Config": {"Labels": labels}}


class FakeService:
    """Minimal stand-in for a docker SDK Service."""

    def __init__(self, service_id, name, replicas=None):
        self.id = service_id
        self.name = name
        mode = (
            {"Replicated": {"Replicas": replicas}}
            if replicas is not None
            else {"Global": {}}
        )
        self.attrs = {"Spec": {"Mode": mode}}


class FakeClient:
    """Minimal stand-in for a docker SDK client exposing api.tasks()."""

    def __init__(self, tasks):
        self.api = self
        self._tasks = tasks

    def tasks(self, filters=None):
        return self._tasks


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
