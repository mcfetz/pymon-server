from plugins.plugin_base import PluginBase
import psutil


class RAMPlugin(PluginBase):
    """
    RAMPlugin erfasst die RAM-Auslastung.

    Gemessene Metriken:

    - virtual_pct (float): Prozentsatz der genutzten virtuellen Arbeitsspeicherressourcen.
    - swap_pct (float): Prozentsatz der genutzten Swap-Ressourcen.
    """

    def get_metrics(self) -> dict | list:
        """
        Ermittelt die virtuelle RAM- und Swap-Auslastung.

        Rückgabewert:
            dict: {
                "virtual_pct": float,
                "swap_pct": float,
            }
        """
        vm = psutil.virtual_memory()
        sm = psutil.swap_memory()

        return {
            "virtual_pct": float(vm.percent),
            "swap_pct": float(sm.percent),
        }

    def get_default_sleep(self) -> int:
        return 30

    def get_plugin_id(self) -> str:
        return "ram"
