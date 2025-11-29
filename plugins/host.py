import platform
import socket
from datetime import datetime
from typing import Dict

import psutil
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
    - swap_total (int): Total swap space in bytes.
    - ip:<interface> (str): IP address of the given network interface (e.g. "ip:eth0").
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

        metrics: dict[str, object] = {
            "hostname": hostname,
            "uptime": uptime_seconds,
            "os": os_name,
            "os_version": os_version,
            "total_ram": int(total_ram),
            "cpu_count": int(cpu_count),
            "cpu_physical_cores": int(cpu_physical_cores),
            "cpu_model": cpu_model,
            "swap_total": int(psutil.swap_memory().total),
        }

        # Add IP addresses per interface: "ip:<interface-name>" -> IP string
        try:
            addrs = psutil.net_if_addrs()
            for if_name, addr_list in addrs.items():
                for addr in addr_list:
                    if addr.family == socket.AF_INET and addr.address:
                        # Skip loopback interface lo with 127.0.0.1
                        if if_name == "lo" and addr.address == "127.0.0.1":
                            continue
                        metrics[f"ip:{if_name}"] = addr.address
                        # If multiple IPv4 addresses per interface exist, the last one wins.
                        # If you want only the first, add a "break" here.
        except Exception:
            # If anything goes wrong while collecting IPs, ignore and return base metrics
            pass

        return metrics

    def get_default_sleep(self) -> int:
        # Default interval for metric polling in seconds
        return 60

    def get_metric_type(self) -> type:
        # get_metrics returns a dict
        return dict

    def get_plugin_id(self) -> str:
        # Return a unique plugin id
        return "host"
