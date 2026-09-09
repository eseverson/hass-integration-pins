"""Integration Pins: run a specific release's copy of a core integration.

Home Assistant loads custom_components/<domain> in preference to the bundled
homeassistant/components/<domain>. This integration manages those override
directories for you: it pulls the integration out of an older release's wheel,
records which core versions you consider it valid for, and raises a Repair
issue when the running core drifts outside that range.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from . import websocket
from .const import (
    DOMAIN,
    PANEL_ICON,
    PANEL_JS_FILE,
    PANEL_STATIC_URL,
    PANEL_TITLE,
    PANEL_URL_PATH,
    PANEL_WEBCOMPONENT,
)
from .manager import PinManager

_LOGGER = logging.getLogger(__name__)

_DATA_STATIC_REGISTERED = f"{DOMAIN}_static_registered"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    manager = PinManager(hass)
    hass.data[DOMAIN] = manager
    await manager.async_setup()

    websocket.async_register(hass)
    await _async_register_panel(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)
    hass.data.pop(DOMAIN, None)
    return True


async def _async_register_panel(hass: HomeAssistant) -> None:
    panel_dir = Path(__file__).parent / "panel"
    if not hass.data.get(_DATA_STATIC_REGISTERED):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(PANEL_STATIC_URL, str(panel_dir), cache_headers=False)]
        )
        hass.data[_DATA_STATIC_REGISTERED] = True

    # Cache-bust on our own version so the browser picks up panel updates.
    from homeassistant.loader import async_get_integration

    integration = await async_get_integration(hass, DOMAIN)
    module_url = f"{PANEL_STATIC_URL}/{PANEL_JS_FILE}?v={integration.version}"

    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=PANEL_WEBCOMPONENT,
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        module_url=module_url,
        embed_iframe=False,
        require_admin=True,
    )
