from abc import ABC, abstractmethod
from typing import Union

# Mögliche Rückgabetypen für get_metric
MetricType = Union[bool, int, float, str]

class PluginBase(ABC):
    @abstractmethod
    def get_metric(self) -> MetricType:
        """
        Liefert einen Metrik-Wert zurück. 
        Der Rückgabetyp kann bool, int, float oder str sein.
        """
        pass
