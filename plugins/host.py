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
    """
    def get_metrics(self) -> dict | list:
        """
        Sammelt Metriken des Hosts und liefert diese als Dictionary zurück.

        Rückgabewert:
            dict: {
                "hostname": str,
                "uptime": float,
                "os": str,
                "os_version": str
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

        return {
            "hostname": hostname,
            "uptime": uptime_seconds,
            "os": os_name,
            "os_version": os_version,
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
