import subprocess
from plugins.plugin_base import PluginBase
import platform

class PingPlugin(PluginBase):
    """
    PingPlugin führt Pings zu mehreren Servern durch.

    Gemessene Metriken:

    - Für jeden Host wird ein Dictionary zurückgegeben, wobei der Schlüssel der Hostname
      und der Wert ein bool ist, der angibt, ob der Ping erfolgreich war (True) oder nicht (False).
    """
    def get_metrics(self) -> dict | list:
        """
        Führt einen Ping zu jedem Server in der Config-Liste 'hosts' aus.
        
        Rückgabewert:
            dict: {
                "<host1>": bool,
                "<host2>": bool,
                ...
            }
        """
        results = {}
        hosts = self.config.get("hosts", [])

        for host in hosts:
            # Wähle den richtigen Befehl basierend auf dem Betriebssystem
            param = "-n" if platform.system().lower()=="windows" else "-c"
            try:
                output = subprocess.check_output(["ping", param, "1", host],
                                                 stderr=subprocess.STDOUT,
                                                 universal_newlines=True)
                results[host] = True
            except subprocess.CalledProcessError:
                results[host] = False
        return results

    def get_metric_type(self) -> type:
        return dict

    def get_default_sleep(self) -> int:
        return 60

    def get_plugin_id(self) -> str:
        return "ping"
