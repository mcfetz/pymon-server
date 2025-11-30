import subprocess
import sys

from plugins.plugin_base import PluginBase


class ServicesPlugin(PluginBase):
    """
    ServicesPlugin prüft den Status von Systemdiensten.

    Gemessene Metriken:

    - Für jeden bekannten Dienst wird ein Dictionary-Eintrag erzeugt:
      key: Dienstname (str)
      value: aktueller Status (str), z.B. "running", "stopped", "failed", "unknown"
    """

    def __init__(self, config: dict):
        super().__init__(config)
        # Liste der zu prüfenden Services; wenn leer, werden alle bekannten Services abgefragt (systemabhängig)
        # Beispiel in agents.toml:
        # [agent1.services]
        # services = ["ssh", "cron"]
        # sleep = 60
        self.services: list[str] = config.get("services", [])
        self._sleep = int(config.get("sleep", 60))

    def _get_service_status_systemd(self, service: str) -> str:
        """
        Ermittelt den Status eines systemd-Services via 'systemctl is-active'.
        """
        try:
            result = subprocess.run(
                ["systemctl", "is-active", service],
                capture_output=True,
                text=True,
                check=False,
            )
            status = result.stdout.strip()
            if not status:
                status = "unknown"
            return status
        except Exception:
            return "unknown"

    def _list_systemd_services(self) -> list[str]:
        """
        Liefert eine Liste aller bekannten systemd-Services (vereinfachte Variante).
        Wird nur verwendet, wenn keine explizite Serviceliste konfiguriert ist.
        """
        try:
            result = subprocess.run(
                ["systemctl", "list-units", "--type=service", "--all", "--no-legend", "--no-pager"],
                capture_output=True,
                text=True,
                check=False,
            )
            services: list[str] = []
            for line in result.stdout.splitlines():
                parts = line.split()
                if not parts:
                    continue
                unit = parts[0]
                if unit.endswith(".service"):
                    services.append(unit)
            return services
        except Exception:
            return []

    def get_metrics(self) -> dict | list:
        """
        Liefert für jeden Dienst den aktuellen Status.
        """
        metrics: dict[str, str] = {}

        # Aktuell unterstützen wir primär systemd-basierte Systeme (Linux).
        # Auf anderen Systemen wird ein leeres Dict zurückgegeben.
        if not sys.platform.startswith("linux"):
            return metrics

        services = self.services
        if not services:
            services = self._list_systemd_services()

        for svc in services:
            status = self._get_service_status_systemd(svc)
            metrics[svc] = status

        return metrics

    def get_metric_type(self) -> type:
        # Status ist ein String
        return str

    def get_plugin_id(self) -> str:
        return "services"
