from plugins.plugin_base import PluginBase
import psutil


class CpuPlugin(PluginBase):
    def get_metrics(self) -> dict | list:
        """
        Gibt die aktuelle CPU-Auslastung in Prozent zurück.
        """
        return {"percent": psutil.cpu_percent(interval=1)}

    def get_default_sleep(self) -> int:
        return 10

    def get_metric_type(self) -> type:
        return float
