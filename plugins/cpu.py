from plugins.plugin_base import PluginBase
import psutil


class CpuPlugin(PluginBase):
    """
    CpuPlugin erfasst die CPU-Auslastung.

    Gemessene Metriken:
    
    - percent (float): Die aktuelle CPU-Auslastung in Prozent.
    """
    def get_metrics(self) -> dict | list:
        """
        Ermittelt die aktuelle CPU-Auslastung in Prozent.

        Rückgabewert:
            dict: {"percent": float}
        """
        return {"percent": psutil.cpu_percent(interval=1)}

    def get_default_sleep(self) -> int:
        return 10

    def get_metric_type(self) -> type:
        return float
