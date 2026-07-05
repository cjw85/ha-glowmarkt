"""The Glowmarkt integration."""

from __future__ import annotations

import logging

import requests

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .const import CONF_DAILY_INTERVAL, CONF_TARIFF_INTERVAL, DOMAIN
from .glow_api import GlowClient
from .history import async_history_statistics_loop

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Glowmarkt from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    try:
        glowmarkt = await hass.async_add_executor_job(
            GlowClient, entry.data["username"], entry.data["password"]
        )
    except requests.exceptions.HTTPError as ex:
        status_code = getattr(ex.response, "status_code", "unknown")
        _LOGGER.error(
            "HTTP error during API authentication: Status Code %s - %s",
            status_code,
            ex,
        )
        if ex.response is not None and ex.response.status_code in (401, 403):
            raise ConfigEntryAuthFailed("Stored Glow credentials were rejected") from ex
        raise ConfigEntryNotReady(f"HTTP Error: {status_code}") from ex
    except requests.Timeout as ex:
        _LOGGER.error("Timeout during API authentication: %s", ex)
        raise ConfigEntryNotReady(f"Timeout: {ex}") from ex
    except requests.exceptions.ConnectionError as ex:
        _LOGGER.error("Connection error during API authentication: %s", ex)
        raise ConfigEntryNotReady(f"Cannot connect: {ex}") from ex
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.exception("Unexpected exception during API authentication: %s", ex)
        raise ConfigEntryNotReady(f"Unexpected exception: {ex}") from ex

    hass.data[DOMAIN][entry.entry_id] = {
        "client": glowmarkt,
        CONF_DAILY_INTERVAL: entry.options.get(
            CONF_DAILY_INTERVAL, entry.data.get(CONF_DAILY_INTERVAL, 15)
        ),
        CONF_TARIFF_INTERVAL: entry.options.get(
            CONF_TARIFF_INTERVAL, entry.data.get(CONF_TARIFF_INTERVAL, 60)
        ),
        "history_task": None,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    hass.data[DOMAIN][entry.entry_id]["history_task"] = hass.async_create_task(
        async_history_statistics_loop(hass, glowmarkt)
    )
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        if history_task := hass.data[DOMAIN][entry.entry_id].get("history_task"):
            history_task.cancel()
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await hass.config_entries.async_reload(entry.entry_id)
