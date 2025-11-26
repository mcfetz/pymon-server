from abc import ABC, abstractmethod
from typing import Union

MetricType = Union[bool, int, float, str]


class PluginBase(ABC):
    @abstractmethod
    def get_metric(self) -> MetricType:
        pass

    @abstractmethod
    def get_metric_type(self) -> type:
        pass

    @abstractmethod
    def get_default_sleep(self) -> int:
        pass
