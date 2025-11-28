import subprocess
from plugins.plugin_base import PluginBase
import platform
import re


class PingPlugin(PluginBase):
    """
    PingPlugin führt Pings zu mehreren Servern durch.

    Gemessene Metriken:

    - Für jeden Host wird ein Dictionary zurückgegeben, wobei der Schlüssel der Hostname
      und der Wert ein bool ist, der angibt, ob der Ping erfolgreich war (True) oder nicht (False).
    """

    def get_metrics(self) -> dict | list:
        """
        Führt mehrere Pings zu jedem Server in der Config-Liste 'hosts' aus
        und ermittelt für jeden den Erfolg und die durchschnittliche Ping-Zeit.

        Rückgabewert:
            dict: {
                "<host1>": {"success": bool, "avg_time": float oder None},
                "<host2>": {"success": bool, "avg_time": float oder None},
                ...
            }
        """
        results = {}
        hosts = self.config.get("hosts", [])

        for host in hosts:
            param = "-n" if platform.system().lower() == "windows" else "-c"
            count = "4"
            try:
                output = subprocess.check_output(
                    ["ping", param, count, host],
                    stderr=subprocess.STDOUT,
                    universal_newlines=True,
                )
                success = True
                avg_time = None
                if platform.system().lower() == "windows":
                    match = re.search(r"Durchschnitt = (\d+)ms", output)
                    if match:
                        avg_time = float(match.group(1))
                else:
                    match = re.search(r"rtt [\w/]+ = [\d\.]+/([\d\.]+)/", output)
                    if match:
                        avg_time = float(match.group(1))
            except subprocess.CalledProcessError:
                success = False
                avg_time = None
            results[host] = {"success": success, "avg_time": avg_time}
        return results

    def get_default_sleep(self) -> int:
        return 60

    def get_plugin_id(self) -> str:
        return "ping"
