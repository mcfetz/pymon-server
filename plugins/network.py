import time
import psutil

from plugins.plugin_base import PluginBase


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
        # state for throughput calculation
        self._last_counters: dict[str, psutil._common.snetio] | None = None
        self._last_time: float | None = None

    def get_metrics(self) -> dict | list:
        """
        Liefert ein Dictionary mit Bytes gesendet/empfangen pro Interface.
        """
        counters = psutil.net_io_counters(pernic=True)
        metrics: dict[str, float] = {}

        now = time.time()
        last_counters = self._last_counters
        last_time = self._last_time

        for ifname, stats in counters.items():
            # kumulative Werte
            metrics[f"{ifname}:bytes_sent"] = float(stats.bytes_sent)
            metrics[f"{ifname}:bytes_recv"] = float(stats.bytes_recv)

            # Durchsatz nur berechnen, wenn wir eine vorherige Messung haben
            if last_counters is not None and last_time is not None:
                prev = last_counters.get(ifname)
                if prev is not None:
                    dt = now - last_time
                    if dt > 0:
                        tx_rate = (stats.bytes_sent - prev.bytes_sent) / dt
                        rx_rate = (stats.bytes_recv - prev.bytes_recv) / dt
                        metrics[f"{ifname}:tx_bytes_per_sec"] = float(tx_rate)
                        metrics[f"{ifname}:rx_bytes_per_sec"] = float(rx_rate)

        # aktuellen Zustand für nächste Messung merken
        self._last_counters = counters
        self._last_time = now

        return metrics

    def get_default_sleep(self) -> int:
        return self._sleep

    def get_metric_type(self) -> type:
        # Bytes als float
        return float

    def get_plugin_id(self) -> str:
        return "network"
