ram
import psutil
from plugin_base import PluginBase  # Bei Bedarf: from .plugin_base import PluginBase

class RAMPlugin(PluginBase):
    def get_metric(self) -> float:
        # Ermittelt die RAM-Auslastung in Prozent
        return psutil.virtual_memory().percent
