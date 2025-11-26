from plugins.plugin_base import PluginBase
import psutil


class RAMPlugin(PluginBase):
    def get_metric(self) -> float:
        # Ermittelt die RAM-Auslastung in Prozent
        return psutil.virtual_memory().percent

    def get_default_sleep(self) -> int:
        return 30

    def get_metric_type(self) -> type:
        return float
