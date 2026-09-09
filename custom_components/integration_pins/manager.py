"""Orchestrates pins: store + filesystem + repairs."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any

from homeassistant import loader
from homeassistant.const import __version__ as CORE_VERSION
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import pinner
from .const import (
    DOMAIN,
    MARKER_FILE,
    ISSUE_FOREIGN,
    ISSUE_MISSING,
    ISSUE_OUT_OF_RANGE,
    STATUS_ACTIVE,
    STATUS_FOREIGN,
    STATUS_MISSING,
    STATUS_OUT_OF_RANGE,
    STATUS_PENDING,
)
from .pinner import PinError
from .store import Pin, PinStore

_LOGGER = logging.getLogger(__name__)

SIGNAL_UPDATED = f"{DOMAIN}_updated"


class PinManager:
    """Single instance stored in hass.data[DOMAIN]."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.store = PinStore(hass)
        # Set when files were removed; pending pins are derived from the loader instead.
        self._removal_pending = False
        self._dependents: dict[str, list[str]] | None = None
        self._lock = asyncio.Lock()
        self._listeners: list[callback] = []

    # -- lifecycle -----------------------------------------------------------

    async def async_setup(self) -> None:
        await self.store.async_load()
        await self.async_check()

    @callback
    def async_add_listener(self, listener) -> callback:
        self._listeners.append(listener)

        @callback
        def _remove() -> None:
            self._listeners.remove(listener)

        return _remove

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    # -- reads ---------------------------------------------------------------

    async def async_snapshot(self) -> dict[str, Any]:
        """Everything the panel needs in one round-trip."""
        config_dir = self.hass.config.config_dir
        loaded = await loader.async_get_custom_components(self.hass)
        pins = []
        for pin in self.store.pins.values():
            info = await self.hass.async_add_executor_job(
                pinner.inspect_override, pin.domain, config_dir
            )
            status = self._status_for(pin, info, loaded.get(pin.domain))
            pins.append(
                {
                    **pin.as_dict(),
                    "status": status,
                    "in_range": pinner.version_in_range(CORE_VERSION, pin.core_range),
                    "override": info,
                }
            )
        pins.sort(key=lambda p: p["domain"])

        unmanaged = await self.hass.async_add_executor_job(
            pinner.list_unmanaged_overrides, config_dir, set(self.store.pins)
        )
        custom = await self.hass.async_add_executor_job(
            pinner.list_custom_integrations, config_dir, set(self.store.pins) | {DOMAIN}
        )
        retired = await self.hass.async_add_executor_job(pinner.list_retired, config_dir)
        return {
            "core_version": CORE_VERSION,
            "restart_required": self._removal_pending
            or any(p["status"] == STATUS_PENDING for p in pins),
            "pins": pins,
            "unmanaged_overrides": unmanaged,
            "custom_integrations": custom,
            "retired": retired,
        }

    async def async_core_domains(self) -> list[str]:
        return await self.hass.async_add_executor_job(pinner.list_core_domains)

    @callback
    def _in_use(self) -> set[str]:
        """Domains this instance is running.

        Loaded components cover YAML and dependency-pulled integrations; config entry
        domains cover ones whose setup failed and so never reached hass.config.components.
        """
        return set(self.hass.config.components) | {
            entry.domain for entry in self.hass.config_entries.async_entries()
        }

    @callback
    def async_in_use_domains(self, core_domains: list[str]) -> list[str]:
        # hass.config.components also holds "sensor.hue"-style platform entries; those
        # never match a core directory name, so intersecting with `core` drops them.
        return sorted((self._in_use() & set(core_domains)) - {DOMAIN})

    async def async_domain_info(self, domain: str) -> dict[str, Any]:
        """What else would be affected by pinning this domain."""
        domain = domain.strip().lower()
        if self._dependents is None:
            self._dependents = await self.hass.async_add_executor_job(pinner.core_dependents)
        dependents = self._dependents.get(domain, [])
        in_use = self._in_use()
        return {
            "domain": domain,
            "dependents": dependents,
            "loaded_dependents": [d for d in dependents if d in in_use],
        }

    async def async_compare(self, domain: str, version: str) -> dict[str, Any]:
        """Which files of an integration differ between a release and the running code."""
        domain = domain.strip().lower()
        version = version.strip()
        pinner.parse_version(version)
        session = async_get_clientsession(self.hass)

        wheel = await pinner.async_get_wheel(session, version)
        candidate = await pinner.async_component_entries(session, wheel, domain)
        if not candidate:
            raise PinError(f"Home Assistant {version} has no core integration '{domain}'")

        base, compared_with = await self._async_baseline(domain, session)
        result = pinner.compare_digests(base, candidate)
        return {
            **result,
            "domain": domain,
            "version": version,
            "compared_with": compared_with,
            "identical": not (result["added"] or result["removed"] or result["changed"]),
        }

    async def _async_baseline(
        self, domain: str, session: Any
    ) -> tuple[dict[str, tuple[int, int]], str]:
        """The digest of whatever code this instance is running for `domain`, and its name."""
        config_dir = self.hass.config.config_dir
        info = await self.hass.async_add_executor_job(pinner.inspect_override, domain, config_dir)
        pin = self.store.get(domain)
        marker = info.get("marker") or {}

        if (
            info.get("present")
            and pin is not None
            and pin.source == "pypi"
            and marker.get("pinned_version") == pin.pinned_version
        ):
            # The override on disk was extracted from a release, but pinning rewrote its
            # manifest and added a marker; that release's own wheel is the honest baseline.
            base_wheel = await pinner.async_get_wheel(session, pin.pinned_version)
            entries = await pinner.async_component_entries(session, base_wheel, domain)
            if entries:
                return entries, f"pinned {pin.pinned_version}"

        if info.get("present"):
            path = pinner.custom_components_dir(config_dir) / domain
            digest = await self.hass.async_add_executor_job(
                pinner.local_component_digest, path, {MARKER_FILE}
            )
            return digest, f"custom_components/{domain}"

        path = pinner.core_components_dir() / domain
        digest = await self.hass.async_add_executor_job(pinner.local_component_digest, path)
        return digest, f"core {CORE_VERSION}"

    async def async_versions(self, include_prereleases: bool) -> list[str]:
        session = async_get_clientsession(self.hass)
        return await pinner.async_list_versions(session, include_prereleases)

    @staticmethod
    def _status_for(
        pin: Pin, info: dict[str, Any], integration: loader.Integration | None
    ) -> str:
        """Classify a pin. `integration` is the custom integration Home Assistant loaded."""
        if not info.get("present"):
            return STATUS_MISSING
        marker = info.get("marker") or {}
        if marker.get("managed_by") != "integration_pins" or marker.get(
            "pinned_version"
        ) != pin.pinned_version:
            return STATUS_FOREIGN
        # Custom integrations are scanned once at startup, so an override written since
        # then is on disk but not running yet -- as is a re-pin over a loaded override.
        if integration is None or str(integration.version) != pin.pinned_version:
            return STATUS_PENDING
        if not pinner.version_in_range(CORE_VERSION, pin.core_range):
            return STATUS_OUT_OF_RANGE
        return STATUS_ACTIVE

    # -- writes --------------------------------------------------------------

    async def async_pin(self, domain: str, version: str, core_range: str, reason: str) -> Pin:
        domain = domain.strip().lower()
        version = version.strip()
        pinner.parse_version(version)
        pinner.parse_range(core_range)
        if domain == DOMAIN:
            raise PinError("Refusing to pin integration_pins itself")

        async with self._lock:
            core_domains = await self.async_core_domains()
            if domain not in core_domains:
                raise PinError(
                    f"'{domain}' is not a core integration in the running Home Assistant"
                )
            existing = self.store.get(domain)
            session = async_get_clientsession(self.hass)
            wheel = await pinner.async_get_wheel(session, version)
            _LOGGER.info(
                "Pinning %s to Home Assistant %s (%.1f MB wheel)",
                domain,
                version,
                wheel.size / 1e6,
            )
            with tempfile.TemporaryDirectory(prefix="integration_pins.") as tmp:
                wheel_path = Path(tmp) / f"homeassistant-{version}.whl"
                await pinner.async_download(session, wheel, wheel_path)
                await self.hass.async_add_executor_job(
                    pinner.extract_component,
                    wheel_path,
                    domain,
                    version,
                    self.hass.config.config_dir,
                    CORE_VERSION,
                )
            pin = Pin(
                domain=domain,
                pinned_version=version,
                core_range=core_range.strip(),
                reason=reason.strip(),
                core_at_pin=CORE_VERSION,
                source="pypi",
                created=existing.created if existing else Pin.__dataclass_fields__["created"].default_factory(),
            )
            await self.store.async_set(pin)
        await self.async_check()
        return pin

    async def async_adopt(
        self, domain: str, pinned_version: str, core_range: str, reason: str
    ) -> Pin:
        """Take over an override directory that already exists (manual copy, etc.)."""
        domain = domain.strip().lower()
        pinner.parse_version(pinned_version)
        pinner.parse_range(core_range)
        async with self._lock:
            path = pinner.custom_components_dir(self.hass.config.config_dir) / domain
            if not await self.hass.async_add_executor_job(path.is_dir):
                raise PinError(f"custom_components/{domain} does not exist")
            await self.hass.async_add_executor_job(
                pinner.write_marker, path, domain, pinned_version, CORE_VERSION
            )
            pin = Pin(
                domain=domain,
                pinned_version=pinned_version,
                core_range=core_range.strip(),
                reason=reason.strip(),
                core_at_pin=CORE_VERSION,
                source="adopted",
            )
            await self.store.async_set(pin)
        await self.async_check()
        return pin

    async def async_update(self, domain: str, core_range: str | None, reason: str | None) -> Pin:
        pin = self.store.get(domain)
        if pin is None:
            raise PinError(f"No pin for '{domain}'")
        if core_range is not None:
            pinner.parse_range(core_range)
            pin.core_range = core_range.strip()
        if reason is not None:
            pin.reason = reason.strip()
        await self.store.async_set(pin)
        await self.async_check()
        return pin

    async def async_unpin(self, domain: str, keep_files: bool = False) -> None:
        pin = self.store.get(domain)
        if pin is None:
            raise PinError(f"No pin for '{domain}'")
        async with self._lock:
            if not keep_files:
                # A pypi pin records the release it came from, so the files can always be
                # fetched again; an adopted one was placed by hand and exists nowhere else.
                keep_a_copy = pin.source != "pypi"
                retired = await self.hass.async_add_executor_job(
                    pinner.remove_override,
                    domain,
                    pin.pinned_version,
                    self.hass.config.config_dir,
                    keep_a_copy,
                )
                if retired:
                    _LOGGER.info("Retired override for %s to %s", domain, retired)
                else:
                    _LOGGER.info("Deleted override for %s (re-pinnable from PyPI)", domain)
                self._removal_pending = True
            await self.store.async_remove(domain)
        for issue in (ISSUE_OUT_OF_RANGE, ISSUE_MISSING, ISSUE_FOREIGN):
            ir.async_delete_issue(self.hass, DOMAIN, f"{issue}_{domain}")
        await self.async_check()

    async def async_delete_retired(self, name: str) -> None:
        await self.hass.async_add_executor_job(
            pinner.delete_retired, self.hass.config.config_dir, name
        )
        self._notify()

    async def async_clear_retired(self) -> int:
        removed = await self.hass.async_add_executor_job(
            pinner.clear_retired, self.hass.config.config_dir
        )
        self._notify()
        return removed

    # -- checks / repairs ----------------------------------------------------

    async def async_check(self) -> None:
        """Re-evaluate every pin against the running core and sync Repair issues."""
        snapshot = await self.async_snapshot()
        for pin in snapshot["pins"]:
            domain = pin["domain"]
            status = pin["status"]
            wanted = {
                STATUS_OUT_OF_RANGE: ISSUE_OUT_OF_RANGE,
                STATUS_MISSING: ISSUE_MISSING,
                STATUS_FOREIGN: ISSUE_FOREIGN,
            }.get(status)
            for issue in (ISSUE_OUT_OF_RANGE, ISSUE_MISSING, ISSUE_FOREIGN):
                issue_id = f"{issue}_{domain}"
                if issue == wanted:
                    ir.async_create_issue(
                        self.hass,
                        DOMAIN,
                        issue_id,
                        is_fixable=False,
                        severity=ir.IssueSeverity.WARNING,
                        translation_key=issue,
                        translation_placeholders={
                            "domain": domain,
                            "pinned_version": pin["pinned_version"],
                            "core_range": pin["core_range"] or "(any)",
                            "core_version": CORE_VERSION,
                        },
                    )
                else:
                    ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            if status != STATUS_ACTIVE:
                _LOGGER.warning(
                    "Integration pin %s (from %s) is %s on core %s",
                    domain,
                    pin["pinned_version"],
                    status,
                    CORE_VERSION,
                )
        self._notify()
