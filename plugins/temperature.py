import psutil

from plugins.plugin_base import PluginBase


class TemperaturePlugin(PluginBase):
    """
    TemperaturePlugin erfasst alle verfügbaren Temperatur-Sensoren des Systems.

    Gemessene Metriken:

    - Für jeden Sensor (bzw. jeden Eintrag in psutil.sensors_temperatures())
      wird ein Dictionary-Eintrag erzeugt:
        key: "<sensor_name>[:<label>]" (z.B. "coretemp:Package id 0")
        value: aktuelle Temperatur in Grad Celsius (float)
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self._sleep = int(config.get("sleep", 30))

    def get_metrics(self) -> dict | list:
        """
        Liefert ein Dictionary aller verfügbaren Temperatur-Sensoren.
        """
        temps = psutil.sensors_temperatures(fahrenheit=False)
        metrics: dict[str, float] = {}

        for name, entries in temps.items():
            for entry in entries:
                label = entry.label or ""
                key = f"{name}:{label}" if label else name
                if entry.current is not None:
                    metrics[key] = float(entry.current)

        return metrics

    def get_metric_type(self) -> type:
        return float

    def get_plugin_id(self) -> str:
        return "temperature"
