import psutil
from plugins.plugin_base import PluginBase


class DiskUsagePlugin(PluginBase):
    def get_metrics(self) -> dict:
        partitions = psutil.disk_partitions()
        usage = {}
        for partition in partitions:
            try:
                du = psutil.disk_usage(partition.mountpoint)
                usage[partition.mountpoint] = du.percent
            except Exception:
                usage[partition.mountpoint] = None
        return usage

    def get_metric_type(self) -> type:
        return dict

    def get_default_sleep(self) -> int:
        return 300
