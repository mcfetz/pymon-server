from abc import ABC, abstractmethod


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
