#!/usr/bin/env python3
"""docker_host.py — Docker host & swarm statistics. Requires docker SDK.

Optional container image update check:
When the 'check_updates' flag is enabled, the image tag of every running
container is compared against the registry that provided it. If the
registry's current manifest digest for that tag differs from the locally
pulled digest, a newer version is available.

Emitted per running container:
  container:<name>:image_outdated   1 = newer version available,
                                    0 = up-to-date,
                                   -1 = could not verify (registry/auth/network)
  container:<name>:image            the image reference in use (string)

Summaries:
  containers_updates_available / containers_updates_current / containers_updates_unchecked

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
    'description': 'Docker host, swarm statistics and container image update checks',
    'fields': [
        {'key': 'sleep', 'label': 'Interval (s)', 'type': 'number', 'default': 60, 'min': 5},
        {'key': 'base_url', 'label': 'Docker socket URL', 'type': 'string', 'default': '', 'optional': True},
        {'key': 'check_updates', 'label': 'Check container image updates in registry', 'type': 'boolean', 'default': False, 'optional': True},
        {'key': 'check_updates_interval_min', 'label': 'Re-check registry interval (min)', 'type': 'number', 'default': 360, 'min': 5, 'optional': True},
        {'key': 'registry_username', 'label': 'Registry username (optional)', 'type': 'string', 'default': '', 'optional': True},
        {'key': 'registry_password', 'label': 'Registry password (optional)', 'type': 'string', 'default': '', 'optional': True},
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


def _check_one(cache, c, interval_min, username, password):
    cid = c.id
    name = c.name
    try:
        image = c.image
        ref = ""
        split = None
        for t in image.tags or []:
            s = _split_ref(t)
            if s:
                split = s
                ref = t
                break
        if not split:
            return cid, name, ref, -1
        local = _local_digest(image)
        if not local:
            return cid, name, ref, -1
        cached = cache.get(cid)
        if cached and cached.get("local") == local and time.time() - cached.get("t", 0) < interval_min * 60:
            return cid, name, ref, cached.get("status", -1)
        registry, repository, tag = split
        remote = _remote_digest(registry, repository, tag, username, password)
        status = -1
        if remote:
            status = 0 if remote.strip().lower() == local.strip().lower() else 1
        cache[cid] = {"t": time.time(), "local": local, "status": status}
        return cid, name, ref, status
    except Exception:
        return cid, name, "", -1


def _update_checks(client, running, metrics, config):
    interval_min = int(config.get("check_updates_interval_min") or 360)
    username = config.get("registry_username", "") or ""
    password = config.get("registry_password", "") or ""
    base_url = config.get("base_url", "")
    cache = _load_cache(base_url)
    deadline = time.time() + UPDATE_BUDGET
    counters = {"outdated": 0, "current": 0, "unchecked": 0}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = []
        for c in running:
            if time.time() >= deadline:
                break
            futures.append(pool.submit(_check_one, cache, c, interval_min, username, password))
        for fut in futures:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                cid, name, ref, status = fut.result(timeout=remaining)
            except Exception:
                continue
            name = name.strip() or cid[:12]
            metrics[f"container:{name}:image_outdated"] = status
            if ref:
                metrics[f"container:{name}:image"] = ref
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

        for c in all_containers:
            name = c.name.strip() or c.short_id
            metrics[f"container:{name}:running"] = 1 if c.status == "running" else 0

        metrics["images_total"] = len(client.images.list())
        metrics["volumes_total"] = len(client.volumes.list())
        metrics["networks_total"] = len(client.networks.list())
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    try:
        services = client.services.list()
        metrics["services_total"] = len(services)
        for svc in services:
            name = svc.name or svc.id
            spec = svc.attrs.get("Spec", {})
            mode = spec.get("Mode", {})
            replicas = None
            if "Replicated" in mode:
                replicas = mode["Replicated"].get("Replicas", 0)
            if replicas is not None:
                metrics[f"service:{name}:replicas"] = replicas
    except APIError:
        pass

    if config.get("check_updates"):
        try:
            _update_checks(client, running, metrics, config)
        except Exception:
            pass

    print(json.dumps(metrics))
