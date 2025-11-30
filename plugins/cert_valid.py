import socket
import ssl
from datetime import datetime, timezone

from plugins.plugin_base import PluginBase


class CertValidPlugin(PluginBase):
    """
    CertValidPlugin prüft die verbleibende Gültigkeit von TLS-Zertifikaten für konfigurierte HTTPS-URLs.

    Gemessene Metriken:

    - Für jede konfigurierte URL wird ein Dictionary-Eintrag erzeugt:
      key: URL (str)
      value: verbleibende Gültigkeit in Tagen (float)
    """

    def __init__(self, config: dict):
        super().__init__(config)
        # Erwartete Config-Struktur (aus agents.toml):
        # [<agentid>.cert_valid]
        # urls = ["https://example.com", "https://example.org"]
        # timeout = 5
        # sleep = 86400
        self.urls: list[str] = config.get("urls", [])
        self.timeout: int = int(config.get("timeout", 5))

    def _get_cert_days_valid(self, url: str) -> float | None:
        """
        Ermittelt die verbleibenden Gültigkeitstage des Zertifikats für die angegebene HTTPS-URL.
        Gibt None zurück, wenn kein Wert ermittelt werden kann.
        """
        if not url.startswith("https://"):
            return None

        hostname = url.removeprefix("https://").split("/", 1)[0]
        port = 443

        context = ssl.create_default_context()
        try:
            with socket.create_connection((hostname, port), timeout=self.timeout) as sock:
                with context.wrap_socket(sock, server_hostname=hostname) as ssock:
                    cert = ssock.getpeercert()
        except OSError:
            return None

        not_after_str = cert.get("notAfter")
        if not not_after_str:
            return None

        # typisches Format: 'Jun  1 12:00:00 2025 GMT'
        try:
            expires = datetime.strptime(not_after_str, "%b %d %H:%M:%S %Y %Z")
            expires = expires.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

        now = datetime.now(timezone.utc)
        delta = expires - now
        return delta.total_seconds() / 86400.0

    def get_metrics(self) -> dict | list:
        """
        Liefert für jede konfigurierte HTTPS-URL die verbleibenden Gültigkeitstage des Zertifikats.
        """
        metrics: dict[str, float] = {}
        for url in self.urls:
            days = self._get_cert_days_valid(url)
            if days is not None:
                metrics[url] = float(days)
        return metrics

    def get_metric_type(self) -> type:
        # Liefert float-Werte (Tage)
        return float

    def get_plugin_id(self) -> str:
        return "cert_valid"
