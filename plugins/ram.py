from plugins.plugin_base import PluginBase
import psutil


class RAMPlugin(PluginBase):
    """
    RAMPlugin erfasst die RAM-Auslastung.

    Gemessene Metriken:

    - virtual_pct (float): Prozentsatz der genutzten virtuellen Arbeitsspeicherressourcen.
    """

    def get_metrics(self) -> dict | list:
        """
        Ermittelt die virtuelle RAM-Auslastung.

        Rückgabewert:
            dict: {"virtual_pct": float}
        """
        return {"virtual_pct": psutil.virtual_memory().percent}

    def get_default_sleep(self) -> int:
        return 30

    def get_plugin_id(self) -> str:
        return "ram"
