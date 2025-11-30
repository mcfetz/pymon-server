from plugins.plugin_base import PluginBase
import psutil


class CpuPlugin(PluginBase):
    def get_metrics(self) -> dict:
        """
        Gibt die aktuelle CPU-Auslastung in Prozent zurück.
        """
        return {"percent": psutil.cpu_percent(interval=1)}

    def get_metric_type(self) -> type:
        return float

    def get_plugin_id(self) -> str:
        return "cpu"
