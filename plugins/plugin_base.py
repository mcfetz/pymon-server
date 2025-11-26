from abc import ABC, abstractmethod
from typing import Union

MetricType = Union[bool, int, float, str]


class PluginBase(ABC):
    @abstractmethod
    def get_metric(self) -> MetricType:
        pass

    @abstractmethod
    def get_metric_type(self) -> type:
        """
        Liefert den Typ der Metrik zurück (z. B. bool, int, float, str).
        """
        pass

    @abstractmethod
    def get_default_sleep(self) -> int:
        """
        Liefert einen Standard-Intervallwert (in Sekunden) als Integer zurück.
        """
        pass
