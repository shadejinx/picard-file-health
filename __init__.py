"""Basic Picard 3 plugin."""

from picard.plugin3.api import PluginApi


def enable(api: PluginApi) -> None:
    """Called when the plugin is enabled.

    Use api to register plugin hooks and access essential Picard APIs.
    """
    api.logger.info("Plugin enabled")


def disable() -> None:
    """Called when the plugin is disabled."""
