"""Integration Pins: run a specific release's copy of a core integration.

Home Assistant loads custom_components/<domain> in preference to the bundled
homeassistant/components/<domain>. This integration manages those override
directories for you: it pulls the integration out of an older release's wheel,
records which core versions you consider it valid for, and raises a Repair
issue when the running core drifts outside that range.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from aiohttp import hdrs, web

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import HomeAssistantView
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


class PanelModuleView(HomeAssistantView):
    """Serve the panel module, telling the browser to check before reusing it.

    Home Assistant's static paths either send a month of `Cache-Control` or none at
    all, and with none a browser falls back to heuristic freshness -- which means an
    updated panel keeps loading the old code until someone hard-reloads. `no-cache`
    still lets the browser store the file; it just has to revalidate first, so the
    steady-state cost is one 304 with an empty body per panel load.
    """

    url = f"{PANEL_STATIC_URL}/{PANEL_JS_FILE}"
    name = f"{DOMAIN}:panel_module"
    requires_auth = False  # loaded by <script type="module">, which cannot send a token

    def __init__(self, path: Path) -> None:
        self._path = path

    async def get(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(self._path, headers={hdrs.CACHE_CONTROL: "no-cache"})


def _module_url(version: str, panel_js: Path) -> str:
    """Cache-bust on the panel's own bytes.

    The manifest version only moves on a release, so keying off it left every edit
    sharing a URL. Hashing the file also avoids busting on a mtime change that did
    not change the content, such as a fresh checkout.
    """
    digest = hashlib.sha256(panel_js.read_bytes()).hexdigest()[:8]
    return f"{PANEL_STATIC_URL}/{PANEL_JS_FILE}?v={version}-{digest}"


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
    panel_js = Path(__file__).parent / "panel" / PANEL_JS_FILE
    if not hass.data.get(_DATA_STATIC_REGISTERED):
        hass.http.register_view(PanelModuleView(panel_js))
        hass.data[_DATA_STATIC_REGISTERED] = True

    from homeassistant.loader import async_get_integration

    integration = await async_get_integration(hass, DOMAIN)
    module_url = await hass.async_add_executor_job(
        _module_url, str(integration.version), panel_js
    )

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
