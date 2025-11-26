from plugins.plugin_base import PluginBase
import psutil


class CpuPlugin(PluginBase):
    def get_metrics(self):
        """
        Gibt die aktuelle CPU-Auslastung in Prozent zurück.
        """
        return psutil.cpu_percent(interval=1)

    def get_default_sleep(self) -> int:
        return 10

    def get_metric_type(self) -> type:
        return float
