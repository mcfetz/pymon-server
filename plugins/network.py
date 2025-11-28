import psutil

from .plugin_base import PluginBase


class NetworkPlugin(PluginBase):
    """
    NetworkPlugin erfasst den Netzwerkdurchsatz (Bytes gesendet/empfangen) pro Interface.

    Gemessene Metriken:

    - Für jedes Interface wird ein Dictionary-Eintrag erzeugt:
      key: "<ifname>:bytes_sent" und "<ifname>:bytes_recv"
      value: Anzahl Bytes (float)
    """

    def __init__(self, config: dict):
        super().__init__(config)
        # optional: sleep-Intervall aus Config, sonst Default
        self._sleep = int(config.get("sleep", 5))

    def get_metrics(self) -> dict | list:
        """
        Liefert ein Dictionary mit Bytes gesendet/empfangen pro Interface.
        """
        counters = psutil.net_io_counters(pernic=True)
        metrics: dict[str, float] = {}

        for ifname, stats in counters.items():
            metrics[f"{ifname}:bytes_sent"] = float(stats.bytes_sent)
            metrics[f"{ifname}:bytes_recv"] = float(stats.bytes_recv)

        return metrics

    def get_default_sleep(self) -> int:
        return self._sleep

    def get_metric_type(self) -> type:
        # Bytes als float
        return float

    def get_plugin_id(self) -> str:
        return "network"
