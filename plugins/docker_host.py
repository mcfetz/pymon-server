#!/usr/bin/env python3
"""docker_host.py — Docker host statistics. Requires docker SDK."""
import json, sys




__schema__ = {'label': 'Docker', 'description': 'Docker host statistics', 'fields': [{'key': 'sleep', 'label': 'Interval (s)', 'type': 'number', 'default': 60, 'min': 5}, {'key': 'base_url', 'label': 'Docker socket URL', 'type': 'string', 'default': '', 'optional': True}]}

try:
    import docker
except ImportError:
    print(json.dumps({"error": "docker SDK not installed"}))
    sys.exit(1)

if __name__ == "__main__":
    config = json.load(sys.stdin)
    base_url = config.get("base_url")

    try:
        client = docker.DockerClient(base_url=base_url) if base_url else docker.from_env()
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    metrics = {}
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

    print(json.dumps(metrics))