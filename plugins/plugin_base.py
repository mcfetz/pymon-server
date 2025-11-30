"""
Dieses Modul definiert die abstrakte Basisklasse PluginBase,
welche von allen Plugin-Klassen implementiert werden muss.

Jedes Plugin sollte folgende Methoden implementieren:
- get_metrics(): Liefert die gesammelten Metriken.
- get_metric_type(): Gibt den Datentyp (z. B. dict, float) der Metriken zurück.
- get_sleep(): Legt den Standardabfrageintervall in Sekunden fest.
- get_plugin_id(): Gibt eine eindeutige Plugin-ID zurück.
"""

from abc import ABC, abstractmethod


class PluginBase(ABC):
    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def get_metrics(self) -> dict:
        pass

    def get_sleep(self) -> int:
        return self.config.get("sleep", 300)

    def get_plugin_id(self) -> str:
        return self.__class__.__name__
