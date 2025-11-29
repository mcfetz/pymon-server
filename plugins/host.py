import platform
import psutil
from datetime import datetime
from plugins.plugin_base import PluginBase

class HostPlugin(PluginBase):
    """
    HostPlugin collects the following metrics:

    - hostname (str): Host name.
    - uptime (float): System uptime in seconds.
    - os (str): Operating system name.
    - os_version (str): Operating system version information.
    - total_ram (float): Total RAM in bytes.
    - cpu_count (int): Number of logical CPUs.
    - cpu_physical_cores (int): Number of physical CPU cores.
    - cpu_model (str): CPU model name.
    - swap_total (float): Total swap space in bytes.
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
            "swap_total": float(psutil.swap_memory().total),
        }

    def get_default_sleep(self) -> int:
        # Default interval for metric polling in seconds
        return 60

    def get_metric_type(self) -> type:
        # get_metrics returns a dict
        return dict

    def get_plugin_id(self) -> str:
        # Return a unique plugin id
        return "host"
