import platform
import psutil
from datetime import datetime
from plugins.plugin_base import PluginBase

class HostPlugin(PluginBase):
    """
    HostPlugin erfasst folgende Metriken:
    
    - hostname (str): Der Name des Hosts.
    - uptime (float): Die Systemlaufzeit in Sekunden.
    - os (str): Name des Betriebssystems.
    - os_version (str): Versionsinformation des Betriebssystems.
    - total_ram (float): Gesamter RAM in Bytes.
    - cpu_count (int): Anzahl der logischen CPUs.
    - cpu_physical_cores (int): Anzahl der physischen CPU-Kerne.
    - cpu_model (str): CPU-Modellbezeichnung.
    """
    def get_metrics(self) -> dict | list:
        """
        Sammelt Metriken des Hosts und liefert diese als Dictionary zurück.

        Rückgabewert:
            dict: {
                "hostname": str,
                "uptime": float,
                "os": str,
                "os_version": str,
                "total_ram": float,
                "cpu_count": int,
                "cpu_physical_cores": int,
                "cpu_model": str,
            }
        """
        # Determine hostname
        hostname = platform.node()

        # Determine operating system
        os_name = platform.system()

        # Determine operating system version
        os_version = platform.version()

        # Determine system uptime (difference between now and boot time)
        boot_time = psutil.boot_time()
        uptime_seconds = (datetime.utcnow() - datetime.fromtimestamp(boot_time)).total_seconds()

        # Determine total RAM size in bytes
        total_ram = psutil.virtual_memory().total

        # Determine CPU information
        cpu_count = psutil.cpu_count(logical=True) or 0
        cpu_physical_cores = psutil.cpu_count(logical=False) or 0
        cpu_model = platform.processor() or ""

        return {
            "hostname": hostname,
            "uptime": uptime_seconds,
            "os": os_name,
            "os_version": os_version,
            "total_ram": float(total_ram),
            "cpu_count": int(cpu_count),
            "cpu_physical_cores": int(cpu_physical_cores),
            "cpu_model": cpu_model,
        }

    def get_default_sleep(self) -> int:
        # Choose a suitable default interval for metric polling
        return 60

    def get_metric_type(self) -> type:
        # get_metrics returns a dict
        return dict

    def get_plugin_id(self) -> str:
        # Return a unique plugin id
        return "host"
