"""
Dieses Modul definiert die abstrakte Basisklasse PluginBase, 
welche von allen Plugin-Klassen implementiert werden muss.

Jedes Plugin sollte folgende Methoden implementieren:
- get_metrics(): Liefert die gesammelten Metriken.
- get_metric_type(): Gibt den Datentyp (z. B. dict, float) der Metriken zurück.
- get_default_sleep(): Legt den Standardabfrageintervall in Sekunden fest.
- get_plugin_id(): Gibt eine eindeutige Plugin-ID zurück.
"""

from abc import ABC, abstractmethod


class PluginBase(ABC):
    def __init__(self, config: dict):
        """
        Initialisiert das Plugin mit der Konfiguration.

        Parameter:
            config (dict): Ein Dictionary, das die Plugin-Konfiguration enthält.
        """
        self.config = config

    @abstractmethod
    def get_metrics(self) -> dict | list:
        pass

    @abstractmethod
    def get_metric_type(self) -> type:
        pass

    @abstractmethod
    def get_default_sleep(self) -> int:
        return 300

    def get_plugin_id(self) -> str:
        return self.__class__.__name__
