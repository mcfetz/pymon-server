import time
import psutil

from plugins.plugin_base import PluginBase


class NetworkPlugin(PluginBase):
    """
    NetworkPlugin collects network throughput (bytes sent/received) per interface.

    Measured metrics:

    - For each interface a dictionary entry is created:
      key: "<ifname>:bytes_sent" and "<ifname>:bytes_recv"
      value: number of bytes (float)
    - Additionally:
      key: "tcp_open_connections"
      value: current number of open TCP connections (float)
    """

    def __init__(self, config: dict):
        super().__init__(config)
        # optional: sleep interval from config, otherwise default
        self._sleep = int(config.get("sleep", 30))
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
            # cumulative values
            metrics[f"{ifname}:bytes_sent"] = float(stats.bytes_sent)
            metrics[f"{ifname}:bytes_recv"] = float(stats.bytes_recv)

            # only calculate throughput if we have a previous measurement
            if last_counters is not None and last_time is not None:
                prev = last_counters.get(ifname)
                if prev is not None:
                    dt = now - last_time
                    if dt > 0:
                        tx_rate = (stats.bytes_sent - prev.bytes_sent) / dt
                        rx_rate = (stats.bytes_recv - prev.bytes_recv) / dt
                        metrics[f"{ifname}:tx_bytes_per_sec"] = float(tx_rate)
                        metrics[f"{ifname}:rx_bytes_per_sec"] = float(rx_rate)

        # store current state for next measurement
        self._last_counters = counters
        self._last_time = now

        # collect number of open TCP connections
        try:
            tcp_conns = psutil.net_connections(kind="tcp")
            metrics["tcp_open_connections"] = len(tcp_conns)
        except Exception:
            # do not set metric on error
            pass

        return metrics

    def get_plugin_id(self) -> str:
        return "network"
