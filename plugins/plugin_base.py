from abc import ABC, abstractmethod
import requests


class PluginBase(ABC):
    @abstractmethod
    def get_metrics(self) -> dict:
        pass

    @abstractmethod
    def get_metric_type(self) -> type:
        pass

    @abstractmethod
    def get_default_sleep(self) -> int:
        return 300

    def get_plugin_id(self) -> str:
        return self.__class__.__name__

    def send_metric(self, server_url: str = "http://localhost:5000/metric") -> requests.Response:
        """
        Sendet die von get_metrics zurückgegebene Metrik als JSON an den /metric Endpoint.
        """
        data = {self.get_plugin_id(): self.get_metrics()}
        try:
            response = requests.post(server_url, json=data)
            return response
        except Exception as e:
            raise RuntimeError(f"Fehler beim Senden der Metriken: {e}")
