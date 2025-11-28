import psutil
from plugins.plugin_base import PluginBase


class DiskUsagePlugin(PluginBase):
    """
    DiskUsagePlugin erfasst Festplattennutzungsdaten.

    Gemessene Metriken:
    
    - Für jede Partition (mountpoint) wird ein Dictionary zurückgegeben, 
      wobei der Schlüssel der Mountpoint und der Wert ein float ist, der den prozentualen Festplattennutzungsgrad angibt.
    """
    def get_metrics(self) -> dict | list:
        """
        Ermittelt die Festplattennutzung für alle Partitionen.

        Rückgabewert:
            list: Eine Liste von Dictionaries, z. B. [{"/": 45.0}, {"/boot": 12.3}]
            - Jeder Wert ist ein float, der den Nutzungs-Prozentsatz darstellt.
        """
        partitions = psutil.disk_partitions()
        usage = []
        for partition in partitions:
            try:
                du = psutil.disk_usage(partition.mountpoint)
                usage.append({partition.mountpoint: du.percent})
            except Exception:
                usage.append({partition.mountpoint: None})
        return usage

    def get_metric_type(self) -> type:
        return dict

    def get_default_sleep(self) -> int:
        return 300
