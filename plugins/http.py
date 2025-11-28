import requests

from plugins.plugin_base import PluginBase


class HttpPlugin(PluginBase):
    """
    HttpPlugin ruft konfigurierte HTTP/HTTPS-URLs ab, prüft den HTTP-Statuscode
    und optional, ob eine bestimmte Zeichenkette im Response-Body enthalten ist.

    Gemessene Metriken:

    - Für jede konfigurierte URL werden folgende Keys erzeugt:
      "<name>:status_code" (int)
      "<name>:ok" (bool als 0/1)
      Optional, wenn expected_string gesetzt ist:
      "<name>:content_ok" (bool als 0/1)
    """

    def __init__(self, config: dict):
        super().__init__(config)
        # Erwartete Config-Struktur:
        # [agent1.http]
        # sleep = 30
        # timeout = 5
        # urls = [
        #   { name = "example", url = "https://example.com", expected_string = "Example Domain" },
        #   { name = "api", url = "https://api.example.com/health" }
        # ]
        self._sleep = int(config.get("sleep", 30))
        self._timeout = int(config.get("timeout", 5))
        self._urls: list[dict[str, str]] = config.get("urls", [])

    def get_metrics(self) -> dict | list:
        metrics: dict[str, float] = {}

        for entry in self._urls:
            name = entry.get("name")
            url = entry.get("url")
            expected = entry.get("expected_string")

            if not name or not url:
                continue

            status_code: int | None = None
            ok = 0.0
            content_ok = None

            try:
                resp = requests.get(url, timeout=self._timeout)
                status_code = resp.status_code

                if expected is not None:
                    # einfache Contains-Prüfung auf dem Text-Body
                    content_ok = 1 if expected in resp.text else 0
            except Exception:
                # Bei Fehlern: status_code bleibt None, ok = 0, content_ok = 0 falls erwartet
                if expected is not None:
                    content_ok = 0.0

            if status_code is not None:
                metrics[f"{name}:status_code"] = int(status_code)
            if content_ok is not None:
                metrics[f"{name}:content_ok"] = content_ok

        return metrics

    def get_default_sleep(self) -> int:
        return self._sleep

    def get_metric_type(self) -> type:
        # Wir liefern numerische Werte (status_code, 0/1-Flags)
        return float

    def get_plugin_id(self) -> str:
        return "http"
