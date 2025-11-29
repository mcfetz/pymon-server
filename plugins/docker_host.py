import docker

from .plugin_base import PluginBase


class DockerHostPlugin(PluginBase):
    """
    DockerHostPlugin collects Docker host statistics.

    Measured metrics (all floats):

    - containers_total: total number of containers (all states)
    - containers_running: number of running containers
    - containers_paused: number of paused containers
    - containers_stopped: number of stopped containers
    - images_total: total number of images
    - volumes_total: total number of volumes
    - networks_total: total number of networks
    """

    def __init__(self, config: dict):
        super().__init__(config)
        base_url = config.get("base_url")
        if base_url:
            self.client = docker.DockerClient(base_url=base_url)
        else:
            self.client = docker.from_env()

    def get_metrics(self) -> dict | list:
        metrics: dict[str, float] = {}

        try:
            # Containers
            all_containers = self.client.containers.list(all=True)
            running_containers = self.client.containers.list(filters={"status": "running"})
            paused_containers = self.client.containers.list(filters={"status": "paused"})
            stopped_containers = self.client.containers.list(filters={"status": "exited"})

            metrics["containers_total"] = float(len(all_containers))
            metrics["containers_running"] = float(len(running_containers))
            metrics["containers_paused"] = float(len(paused_containers))
            metrics["containers_stopped"] = float(len(stopped_containers))

            # Images
            images = self.client.images.list()
            metrics["images_total"] = float(len(images))

            # Volumes
            volumes = self.client.volumes.list()
            metrics["volumes_total"] = float(len(volumes))

            # Networks
            networks = self.client.networks.list()
            metrics["networks_total"] = float(len(networks))

        except Exception:
            # On any Docker error, return whatever we have (possibly empty)
            return metrics

        return metrics

    def get_default_sleep(self) -> int:
        """
        Default polling interval in seconds.
        """
        return int(self.config.get("sleep", 30))

    def get_metric_type(self) -> type:
        """
        All metrics are numeric (float).
        """
        return float

    def get_plugin_id(self) -> str:
        """
        Unique plugin identifier used by the server.
        """
        return "docker_host"
