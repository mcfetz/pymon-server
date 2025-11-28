import psutil
from plugins.plugin_base import PluginBase


class DiskUsagePlugin(PluginBase):
    def get_metrics(self) -> dict | list:
        partitions = psutil.disk_partitions()
        usage = []
        for partition in partitions:
            try:
                du = psutil.disk_usage(partition.mountpoint)
                usage.append({partition.mountpoint: du.percent})
            except Exception:
                usage.append({partition.mountpoint: None})
        return usage

    def get_metric_type(self) -> type:
        return dict

    def get_default_sleep(self) -> int:
        return 300
