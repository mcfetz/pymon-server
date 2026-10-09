#!/usr/bin/env python3
"""docker_host.py — Docker host & swarm statistics. Requires docker SDK.

Optional container image update check:
When the 'check_updates' flag is enabled, the image tag of every running
container is compared against the registry that provided it. If the
registry's current manifest digest for that tag differs from the locally
pulled digest, a newer version is available. A container that is running but
unhealthy is never checked — a crash-looping or healthcheck-failing container
must not be reported as "newer image available", because the image is not the
cause.

Containers without a healthcheck report no health status and are checked by
default. Set 'check_updates_include_unchecked' to false to check only
containers that actually pass their healthcheck instead; note that hosts whose
images ship no HEALTHCHECK then lose nearly all update coverage.

Some fields include per-container resource usage from the Docker stats API:

  container:<name>:cpu_percent       CPU share since the clostest sample (can
                                     exceed 100 across multiple cores)
  container:<name>:mem_usage_bytes   current memory usage
  container:<name>:mem_limit_bytes   container memory limit
  container:<name>:mem_percent       usage/limit in percent
  container:<name>:pids              current number of processes
  container:<name>:io_read_bytes     cumulative block reads since start
  container:<name>:io_write_bytes    cumulative block writes since start
  container:<name>:net_rx_bytes      cumulative received network bytes
  container:<name>:net_tx_bytes      cumulative transmitted network bytes

Emitted per checked container (non-swarm):
  container:<name>:image_outdated   1 = newer version available,
                                     0 = up-to-date,
                                    -1 = could not verify (registry/auth/network)
  container:<name>:image            the image reference in use (string)

Emitted per checked swarm service (one check per service, not per task, so the
metric name stays stable across task reschedules):
  service:<name>:image_outdated     same 1/0/-1 semantics as above
  service:<name>:image              the image reference the service runs

Summaries:
  containers_updates_available / containers_updates_current / containers_updates_unchecked
  containers_updates_skipped       running containers not checked at container
                                   level: unhealthy containers and swarm task
                                   containers (their image is checked per service)

Per-container state:
  container:<name>:running     1 = running, 0 = not (only for non-swarm
                               containers; swarm task transitions would be noise)
  container:<name>:service     the swarm service a task container belongs to

Per swarm service:
  service:<name>:replicas      desired replica count (replicated services)
  service:<name>:tasks_running actual number of running tasks
  service:<name>:up            1 when the service meets its desired replicas
                               (or has a task running for global services)

The registry is only queried when the local image digest changed or the
re-check interval (check_updates_interval_min) elapsed, results are cached
in a per-host file under the temp dir.
"""
import base64
import hashlib
import json
import os
import socket
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

__schema__ = {
    'label': 'Docker',
    'description': 'Docker host, swarm statistics, per-container resource usage and image update checks',
    'fields': [
        {'key': 'sleep', 'label': 'Interval (s)', 'type': 'number', 'default': 60, 'min': 5},
        {'key': 'base_url', 'label': 'Docker socket URL', 'type': 'string', 'default': '', 'optional': True},
        {'key': 'collect_stats', 'label': 'Collect per-container CPU/memory/IO/net', 'type': 'boolean', 'default': True, 'optional': True},
        {'key': 'check_updates', 'label': 'Check container image updates in registry', 'type': 'boolean', 'default': False, 'optional': True},
        {'key': 'check_updates_interval_min', 'label': 'Re-check registry interval (min)', 'type': 'number', 'default': 360, 'min': 5, 'optional': True},
        {'key': 'registry_username', 'label': 'Registry username (optional)', 'type': 'string', 'default': '', 'optional': True},
        {'key': 'registry_password', 'label': 'Registry password (optional)', 'type': 'password', 'default': '', 'optional': True},
    ],
}

try:
    import docker
    from docker.errors import APIError
except ImportError:
    print(json.dumps({"error": "docker SDK not installed"}))
    sys.exit(1)

# The agent kills plugins after PLUGIN_TIMEOUT (30s); leave a margin.
UPDATE_BUDGET = 20.0
TOKEN_TIMEOUT = 6
MANIFEST_TIMEOUT = 8
_ssl_ctx = ssl.create_default_context()

MANIFEST_ACCEPT = ", ".join([
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
])


def _http(url, headers=None, method="GET", username="", password=""):
    req = urllib.request.Request(url, data=None, headers=headers or {}, method=method)
    if username:
        cred = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        req.add_header("Authorization", f"Basic {cred}")
    timeout = TOKEN_TIMEOUT if method != "HEAD" else MANIFEST_TIMEOUT
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), b""
    except Exception:
        return 0, {}, b""


def _token(url, username, password):
    status, _, body = _http(url, username=username, password=password)
    if status != 200:
        return None
    try:
        return json.loads(body.decode("utf-8", "replace")).get("token")
    except Exception:
        return None


def _hub_token(repository, username, password):
    scope = urllib.parse.quote(f"repository:{repository}:pull", safe="")
    return _token("https://auth.docker.io/token?service=registry.docker.io&scope=" + scope, username, password)


def _generic_token(registry, repository, username, password):
    scope = urllib.parse.quote(f"repository:{repository}:pull", safe="")
    return _token(f"https://{registry}/token?service={registry}&scope=" + scope, username, password)


def _remote_digest(registry, repository, ref, username, password):
    if registry in ("registry-1.docker.io", "docker.io", "index.docker.io"):
        token = _hub_token(repository, username, password)
    else:
        token = _generic_token(registry, repository, username, password)
    url = f"https://{registry}/v2/{repository}/manifests/{urllib.parse.quote(ref, safe='')}"
    headers = {"Accept": MANIFEST_ACCEPT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status, resp_headers, _ = _http(url, headers=headers, method="HEAD", username=username, password=password)
    if status not in (200, 201):
        return None
    for key, value in resp_headers.items():
        if key.lower() == "docker-content-digest":
            return value if value else None
    return None


def _local_digest(image):
    for rd in image.attrs.get("RepoDigests") or []:
        if "@" in rd:
            return rd.split("@", 1)[-1]
    return image.attrs.get("Digest")


def _split_ref(ref):
    """Split an image reference into (registry, repository, tag) or None."""
    if not ref or "@" in ref:
        return None
    if ":" in ref.rsplit("/", 1)[-1]:
        repo_part, tag = ref.rsplit(":", 1)
    else:
        repo_part, tag = ref, "latest"
    if not repo_part:
        return None
    if "/" not in repo_part:
        return "registry-1.docker.io", "library/" + repo_part, tag
    first, sep, rest = repo_part.partition("/")
    if sep and first in ("docker.io", "index.docker.io", "registry-1.docker.io"):
        if "/" not in rest:
            return "registry-1.docker.io", "library/" + rest, tag
        return "registry-1.docker.io", rest, tag
    if sep and ("." in first or ":" in first or first == "localhost" or first.isdigit()):
        return first, rest, tag
    return "registry-1.docker.io", repo_part, tag


def _cache_path(base_url):
    host = socket.gethostname()
    key = hashlib.sha256(f"{host}|{base_url}".encode("utf-8")).hexdigest()[:12]
    return os.path.join(tempfile.gettempdir(), f"pymon_docker_updates_{key}.json")


def _load_cache(base_url):
    try:
        with open(_cache_path(base_url), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cache(base_url, cache):
    path = _cache_path(base_url)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, path)
    except OSError:
        pass


def _resolve_image(client, ref):
    """Return the local Image object for a ref, or None if it is not present."""
    if not ref:
        return None
    try:
        return client.images.get(ref)
    except Exception:
        return None


def _check_ref(cache, key, ref, local, interval_min, username, password):
    """Compare a local image digest against the registry for ``ref``.

    Returns 1 (newer version available), 0 (up to date) or -1 (not
    verifiable). The result is cached under ``key`` and reused while the local
    digest is unchanged and the re-check interval has not elapsed.
    """
    split = _split_ref(ref)
    if not split or not local:
        return -1
    cached = cache.get(key)
    if cached and cached.get("local") == local and time.time() - cached.get("t", 0) < interval_min * 60:
        return cached.get("status", -1)
    registry, repository, tag = split
    remote = _remote_digest(registry, repository, tag, username, password)
    status = -1
    if remote:
        status = 0 if remote.strip().lower() == local.strip().lower() else 1
    cache[key] = {"t": time.time(), "local": local, "status": status}
    return status


def _check_one(cache, c, interval_min, username, password):
    """Check one non-swarm container. Returns (namespace, name, ref, status)."""
    name = (c.name or "").strip() or c.short_id or c.id[:12]
    try:
        image = c.image
        ref = ""
        for t in image.tags or []:
            if _split_ref(t):
                ref = t
                break
        status = _check_ref(
            cache, c.id, ref, _local_digest(image), interval_min, username, password
        )
        return "container", name, ref, status
    except Exception:
        return "container", name, "", -1


def _check_service(cache, client, svc, interval_min, username, password):
    """Check a swarm service image once. Returns (namespace, name, ref, status)."""
    name = svc.name or svc.id
    try:
        spec = svc.attrs.get("Spec") or {}
        ref = ((spec.get("TaskTemplate") or {}).get("ContainerSpec") or {}).get(
            "Image", ""
        ) or ""
        image = _resolve_image(client, ref)
        local = _local_digest(image) if image is not None else None
        status = _check_ref(
            cache, "service:" + svc.id, ref, local, interval_min, username, password
        )
        return "service", name, ref, status
    except Exception:
        return "service", name, "", -1


def _update_check_candidates(client, running, include_unchecked=False):
    """Return the running containers that are eligible for an image update check.

    A container that is explicitly unhealthy is never checked: a crash-looping
    or healthcheck-failing container must not be reported as "newer image
    available", because the image is not the cause.

    Containers without a healthcheck report no health status at all. They are
    included only when ``include_unchecked`` is set — on a host where most
    images ship no HEALTHCHECK they would otherwise silently lose update
    coverage.

    Swarm task containers are excluded: their restart during a rollout is
    expected and their image is checked once per service instead (see
    ``_check_service``).

    The Docker ``health`` filter resolves the matching sets in one call instead
    of an inspect per container. If the daemon rejects the filter we fall back
    to a per-container inspect.
    """
    wanted = ["healthy"] + (["none"] if include_unchecked else [])
    eligible = set()
    for value in wanted:
        try:
            eligible |= {c.id for c in client.containers.list(filters={"health": value})}
        except Exception:
            eligible = None
            break
    if eligible is None:
        candidates = []
        for c in running:
            try:
                state = client.api.inspect_container(c.id).get("State") or {}
            except Exception:
                continue
            status = (state.get("Health") or {}).get("Status")
            if status == "healthy" or (include_unchecked and status is None):
                candidates.append(c)
    else:
        candidates = [c for c in running if c.id in eligible]
    return [c for c in candidates if not _swarm_service_id(c)]


def _update_checks(client, candidates, services, metrics, config):
    interval_min = int(config.get("check_updates_interval_min") or 360)
    username = config.get("registry_username", "") or ""
    password = config.get("registry_password", "") or ""
    base_url = config.get("base_url", "")
    cache = _load_cache(base_url)
    deadline = time.time() + UPDATE_BUDGET
    counters = {"outdated": 0, "current": 0, "unchecked": 0}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = []
        for c in candidates:
            if time.time() >= deadline:
                break
            futures.append(pool.submit(_check_one, cache, c, interval_min, username, password))
        for svc in services:
            if time.time() >= deadline:
                break
            futures.append(
                pool.submit(
                    _check_service, cache, client, svc, interval_min, username, password
                )
            )
        for fut in futures:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                namespace, name, ref, status = fut.result(timeout=remaining)
            except Exception:
                continue
            name = name.strip() or "?"
            metrics[f"{namespace}:{name}:image_outdated"] = status
            if ref:
                metrics[f"{namespace}:{name}:image"] = ref
            if status == 1:
                counters["outdated"] += 1
            elif status == 0:
                counters["current"] += 1
            else:
                counters["unchecked"] += 1
    metrics["containers_updates_available"] = counters["outdated"]
    metrics["containers_updates_current"] = counters["current"]
    metrics["containers_updates_unchecked"] = counters["unchecked"]
    _save_cache(base_url, cache)


def _resource_stats(running, metrics, budget=8.0):
    """Collect per-container CPU/memory/IO/net/pids via the Docker stats API.

    Uses the daemon's ``precpu_stats`` so CPU percent is computed without a
    second sample. Each container gets a try/except so one broken stats call
    never fails the whole poll. Stops early once the budget is spent.
    """
    deadline = time.time() + budget
    for c in running:
        if time.time() >= deadline:
            break
        name = c.name.strip() or c.short_id
        try:
            s = c.stats(stream=False)
        except Exception:
            continue

        cpu = s.get("cpu_stats", {}) or {}
        precpu = s.get("precpu_stats", {}) or {}
        cpu_delta = (cpu.get("cpu_usage", {}).get("total_usage", 0)
                     - precpu.get("cpu_usage", {}).get("total_usage", 0))
        sys_delta = (cpu.get("system_cpu_usage", 0)
                     - precpu.get("system_cpu_usage", 0))
        online = cpu.get("online_cpus") or precpu.get("online_cpus") or 1
        if sys_delta > 0 and cpu_delta >= 0:
            metrics[f"container:{name}:cpu_percent"] = round(
                cpu_delta / sys_delta * online * 100.0, 1)

        mem = s.get("memory_stats", {}) or {}
        usage = mem.get("usage")
        limit = mem.get("limit")
        if usage is not None:
            metrics[f"container:{name}:mem_usage_bytes"] = int(usage)
            if limit:
                metrics[f"container:{name}:mem_limit_bytes"] = int(limit)
                metrics[f"container:{name}:mem_percent"] = round(100.0 * usage / limit, 1)
        pids = (mem.get("pids_stats", {}) or {}).get("current")
        if pids is not None:
            metrics[f"container:{name}:pids"] = int(pids)

        read_b = write_b = 0
        for b in (s.get("blkio_stats", {}) or {}).get("io_service_bytes_recursive", []) or []:
            op = (b.get("op") or "").lower()
            if op == "read":
                read_b += b.get("value", 0)
            elif op == "write":
                write_b += b.get("value", 0)
        if read_b or write_b:
            metrics[f"container:{name}:io_read_bytes"] = int(read_b)
            metrics[f"container:{name}:io_write_bytes"] = int(write_b)

        net = s.get("networks", {}) or {}
        rx = sum(((v or {}).get("rx_bytes") or 0) for v in net.values())
        tx = sum(((v or {}).get("tx_bytes") or 0) for v in net.values())
        if rx or tx:
            metrics[f"container:{name}:net_rx_bytes"] = int(rx)
            metrics[f"container:{name}:net_tx_bytes"] = int(tx)


def _swarm_service_id(c):
    return ((c.attrs.get("Config") or {}).get("Labels") or {}).get("com.docker.swarm.service.id")


def _container_states(all_containers, services, metrics):
    """Emit per-container running state.

    Containers that belong to a swarm service are excluded from
    ``container:<name>:running``: their exit/restart is expected during
    rollouts, so a down alarm at container level would be noise. They are
    tracked at the service level instead (see ``_service_states``).
    """
    svc_by_id = {}
    for svc in services:
        svc_by_id[svc.id] = svc
    for c in all_containers:
        name = c.name.strip() or c.short_id
        sid = _swarm_service_id(c)
        if sid:
            svc = svc_by_id.get(sid)
            if svc:
                metrics[f"container:{name}:service"] = svc.name or sid[:12]
            continue
        metrics[f"container:{name}:running"] = 1 if c.status == "running" else 0


def _service_states(client, services, metrics):
    """Emit per-service desired replicas, running tasks and an up flag.

    Task counts are resolved over the whole swarm (one ``api.tasks`` call)
    instead of the local node's containers: a service whose tasks run on
    other nodes must not look down on this node. ``up`` is 1 when a
    replicated service meets its desired replica count (at least one task
    running for global services).
    """
    running_by_svc = {}
    try:
        task_rows = client.api.tasks(filters={"desired-state": "running"})
    except Exception:
        task_rows = []
    for t in task_rows:
        if (t.get("Status") or {}).get("State") == "running":
            sid = t.get("ServiceID", "")
            running_by_svc[sid] = running_by_svc.get(sid, 0) + 1
    for svc in services:
        name = svc.name or svc.id
        spec = svc.attrs.get("Spec", {})
        mode = spec.get("Mode", {})
        replicas = None
        if "Replicated" in mode:
            replicas = mode["Replicated"].get("Replicas", 0)
        if replicas is not None:
            metrics[f"service:{name}:replicas"] = replicas
        running_n = running_by_svc.get(svc.id, 0)
        metrics[f"service:{name}:tasks_running"] = running_n
        if replicas is not None:
            metrics[f"service:{name}:up"] = 1 if running_n >= replicas else 0
        else:
            metrics[f"service:{name}:up"] = 1 if running_n > 0 else 0


if __name__ == "__main__":
    config = json.load(sys.stdin)
    base_url = config.get("base_url")

    try:
        client = docker.DockerClient(base_url=base_url) if base_url else docker.from_env()
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    metrics = {}
    running = []
    try:
        all_containers = client.containers.list(all=True)
        running = [c for c in all_containers if c.status == "running"]
        paused = [c for c in all_containers if c.status == "paused"]
        exited = [c for c in all_containers if c.status == "exited"]
        metrics["containers_total"] = len(all_containers)
        metrics["containers_running"] = len(running)
        metrics["containers_paused"] = len(paused)
        metrics["containers_stopped"] = len(exited)

        metrics["images_total"] = len(client.images.list())
        metrics["volumes_total"] = len(client.volumes.list())
        metrics["networks_total"] = len(client.networks.list())
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    try:
        services = client.services.list()
        metrics["services_total"] = len(services)
    except APIError:
        services = []

    try:
        _container_states(all_containers, services, metrics)
        _service_states(client, services, metrics)
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    if config.get("check_updates"):
        try:
            # Absent or null means "checked": on a host whose images ship no
            # HEALTHCHECK, skipping them would silently drop nearly all
            # coverage, so the permissive default is the safe one.
            raw = config.get("check_updates_include_unchecked")
            include_unchecked = True if raw is None else bool(raw)
            candidates = _update_check_candidates(client, running, include_unchecked)
            metrics["containers_updates_skipped"] = len(running) - len(candidates)
            _update_checks(client, candidates, services, metrics, config)
        except Exception:
            pass

    if config.get("collect_stats", True):
        try:
            _resource_stats(running, metrics)
        except Exception:
            pass

    print(json.dumps(metrics))
