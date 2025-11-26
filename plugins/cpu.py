cpu
from .plugin_base import PluginBase
import psutil

class CpuPlugin(PluginBase):
    def get_metric(self):
        """
        Gibt die aktuelle CPU-Auslastung in Prozent zurück.
        """
        return psutil.cpu_percent(interval=1)
