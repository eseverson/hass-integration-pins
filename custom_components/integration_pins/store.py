"""Persistent pin registry backed by Home Assistant's storage helper."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import STORAGE_KEY, STORAGE_VERSION


@dataclass
class Pin:
    """A single pinned integration."""

    domain: str
    pinned_version: str  # Home Assistant release the override code came from
    core_range: str  # PEP 440 specifier set; "" means "any core version"
    reason: str = ""
    created: str = field(default_factory=lambda: dt_util.utcnow().isoformat())
    core_at_pin: str = ""  # core version that was running when the pin was made
    source: str = "pypi"  # "pypi", "git" or "adopted"
    git_ref: str = ""  # branch, tag or commit that was asked for
    git_sha: str = ""  # commit it resolved to

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Pin:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class PinStore:
    """Load/save pins."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._pins: dict[str, Pin] = {}

    async def async_load(self) -> None:
        data = await self._store.async_load()
        self._pins = {}
        if data:
            for raw in data.get("pins", []):
                pin = Pin.from_dict(raw)
                self._pins[pin.domain] = pin

    async def async_save(self) -> None:
        await self._store.async_save({"pins": [p.as_dict() for p in self._pins.values()]})

    @property
    def pins(self) -> dict[str, Pin]:
        return self._pins

    def get(self, domain: str) -> Pin | None:
        return self._pins.get(domain)

    async def async_set(self, pin: Pin) -> None:
        self._pins[pin.domain] = pin
        await self.async_save()

    async def async_remove(self, domain: str) -> Pin | None:
        pin = self._pins.pop(domain, None)
        if pin is not None:
            await self.async_save()
        return pin
