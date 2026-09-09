"""WebSocket API consumed by the sidebar panel."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .manager import PinManager
from .pinner import PinError

_LOGGER = logging.getLogger(__name__)


def _manager(hass: HomeAssistant) -> PinManager:
    return hass.data[DOMAIN]


def _wrap(func):
    """Turn PinError into a websocket error instead of a traceback."""

    async def wrapper(hass, connection, msg):
        try:
            await func(hass, connection, msg)
        except PinError as err:
            connection.send_error(msg["id"], "pin_error", str(err))
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("integration_pins websocket command failed")
            connection.send_error(msg["id"], "unknown_error", f"{type(err).__name__}: {err}")

    wrapper.__name__ = func.__name__
    return wrapper


@callback
def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_list)
    websocket_api.async_register_command(hass, ws_subscribe)
    websocket_api.async_register_command(hass, ws_core_domains)
    websocket_api.async_register_command(hass, ws_domain_info)
    websocket_api.async_register_command(hass, ws_versions)
    websocket_api.async_register_command(hass, ws_compare)
    websocket_api.async_register_command(hass, ws_pin)
    websocket_api.async_register_command(hass, ws_adopt)
    websocket_api.async_register_command(hass, ws_update)
    websocket_api.async_register_command(hass, ws_unpin)
    websocket_api.async_register_command(hass, ws_check)
    websocket_api.async_register_command(hass, ws_delete_retired)
    websocket_api.async_register_command(hass, ws_clear_retired)


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/list"})
@websocket_api.async_response
@_wrap
async def ws_list(hass, connection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], await _manager(hass).async_snapshot())


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/subscribe"})
@websocket_api.async_response
async def ws_subscribe(hass, connection, msg: dict[str, Any]) -> None:
    """Push a fresh snapshot whenever pins change."""
    manager = _manager(hass)

    @callback
    def _changed() -> None:
        hass.async_create_task(_push())

    async def _push() -> None:
        connection.send_message(
            websocket_api.event_message(msg["id"], await manager.async_snapshot())
        )

    connection.subscriptions[msg["id"]] = manager.async_add_listener(_changed)
    connection.send_result(msg["id"])
    await _push()


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/core_domains"})
@websocket_api.async_response
@_wrap
async def ws_core_domains(hass, connection, msg: dict[str, Any]) -> None:
    manager = _manager(hass)
    domains = await manager.async_core_domains()
    connection.send_result(
        msg["id"], {"domains": domains, "in_use": manager.async_in_use_domains(domains)}
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/domain_info", vol.Required("domain"): str}
)
@websocket_api.async_response
@_wrap
async def ws_domain_info(hass, connection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], await _manager(hass).async_domain_info(msg["domain"]))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/versions",
        vol.Optional("include_prereleases", default=False): bool,
    }
)
@websocket_api.async_response
@_wrap
async def ws_versions(hass, connection, msg: dict[str, Any]) -> None:
    versions = await _manager(hass).async_versions(msg["include_prereleases"])
    connection.send_result(msg["id"], {"versions": versions})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/pin",
        vol.Required("domain"): str,
        vol.Required("version"): str,
        vol.Optional("core_range", default=""): str,
        vol.Optional("reason", default=""): str,
    }
)
@websocket_api.async_response
@_wrap
async def ws_pin(hass, connection, msg: dict[str, Any]) -> None:
    pin = await _manager(hass).async_pin(
        msg["domain"], msg["version"], msg["core_range"], msg["reason"]
    )
    connection.send_result(msg["id"], pin.as_dict())


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/adopt",
        vol.Required("domain"): str,
        vol.Required("pinned_version"): str,
        vol.Optional("core_range", default=""): str,
        vol.Optional("reason", default=""): str,
    }
)
@websocket_api.async_response
@_wrap
async def ws_adopt(hass, connection, msg: dict[str, Any]) -> None:
    pin = await _manager(hass).async_adopt(
        msg["domain"], msg["pinned_version"], msg["core_range"], msg["reason"]
    )
    connection.send_result(msg["id"], pin.as_dict())


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/update",
        vol.Required("domain"): str,
        vol.Optional("core_range"): str,
        vol.Optional("reason"): str,
    }
)
@websocket_api.async_response
@_wrap
async def ws_update(hass, connection, msg: dict[str, Any]) -> None:
    pin = await _manager(hass).async_update(
        msg["domain"], msg.get("core_range"), msg.get("reason")
    )
    connection.send_result(msg["id"], pin.as_dict())


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/unpin",
        vol.Required("domain"): str,
        vol.Optional("keep_files", default=False): bool,
    }
)
@websocket_api.async_response
@_wrap
async def ws_unpin(hass, connection, msg: dict[str, Any]) -> None:
    await _manager(hass).async_unpin(msg["domain"], msg["keep_files"])
    connection.send_result(msg["id"])


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/check"})
@websocket_api.async_response
@_wrap
async def ws_check(hass, connection, msg: dict[str, Any]) -> None:
    await _manager(hass).async_check()
    connection.send_result(msg["id"])


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/delete_retired", vol.Required("name"): str}
)
@websocket_api.async_response
@_wrap
async def ws_delete_retired(hass, connection, msg: dict[str, Any]) -> None:
    await _manager(hass).async_delete_retired(msg["name"])
    connection.send_result(msg["id"])


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/clear_retired"})
@websocket_api.async_response
@_wrap
async def ws_clear_retired(hass, connection, msg: dict[str, Any]) -> None:
    removed = await _manager(hass).async_clear_retired()
    connection.send_result(msg["id"], {"removed": removed})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/compare",
        vol.Required("domain"): str,
        vol.Required("version"): str,
    }
)
@websocket_api.async_response
@_wrap
async def ws_compare(hass, connection, msg: dict[str, Any]) -> None:
    connection.send_result(
        msg["id"], await _manager(hass).async_compare(msg["domain"], msg["version"])
    )
