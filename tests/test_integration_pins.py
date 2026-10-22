"""End-to-end tests against a real Home Assistant core."""

from __future__ import annotations

import asyncio
import builtins
import hashlib
import importlib
import io
import json
import os
import sys
import types
from pathlib import Path

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import __version__ as CORE_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.integration_pins import alias, pinner
from custom_components.integration_pins.const import (
    DOMAIN,
    ISSUE_OUT_OF_RANGE,
    MARKER_FILE,
    RETIRED_DIRNAME,
)

from .conftest import FAKE_DOMAIN, FAKE_VERSION, build_fake_wheel


@pytest.fixture
async def setup(hass: HomeAssistant, tmp_path: Path):
    hass.config.config_dir = str(tmp_path)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _mock_pypi(aioclient_mock, fake_wheel: Path, version: str = FAKE_VERSION, released: str = ""):
    data = fake_wheel.read_bytes()
    aioclient_mock.get(
        f"https://pypi.org/pypi/homeassistant/{version}/json",
        json={
            "urls": [
                {
                    "packagetype": "bdist_wheel",
                    "url": f"https://files.example/ha-{version}.whl",
                    "digests": {"sha256": hashlib.sha256(data).hexdigest()},
                    "size": len(data),
                    "upload_time_iso_8601": released,
                }
            ]
        },
    )
    aioclient_mock.get(f"https://files.example/ha-{version}.whl", content=data)


async def test_setup_registers_panel_and_ws(hass: HomeAssistant, setup, hass_ws_client):
    from homeassistant.components.frontend import DATA_PANELS

    assert "integration-pins" in hass.data[DATA_PANELS]

    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/list"})
    msg = await client.receive_json()
    assert msg["success"]
    assert msg["result"]["core_version"] == CORE_VERSION
    assert msg["result"]["pins"] == []
    assert msg["result"]["restart_required"] is False

    await client.send_json_auto_id({"type": f"{DOMAIN}/core_domains"})
    msg = await client.receive_json()
    assert "sun" in msg["result"]["domains"]
    assert "hue" in msg["result"]["domains"]


async def test_pin_update_unpin_flow(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, fake_wheel, tmp_path
):
    _mock_pypi(aioclient_mock, fake_wheel)
    client = await hass_ws_client(hass)

    # --- pin ---------------------------------------------------------------
    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/pin",
            "domain": FAKE_DOMAIN,
            "version": FAKE_VERSION,
            "core_range": f"=={CORE_VERSION}",
            "reason": "sensor broke",
        }
    )
    msg = await client.receive_json()
    assert msg["success"], msg
    override = tmp_path / "custom_components" / FAKE_DOMAIN
    assert (override / "__init__.py").is_file()
    assert (override / "sensor.py").is_file()
    assert (override / "translations" / "en.json").is_file()
    assert not (override / "__pycache__").exists()
    manifest = json.loads((override / "manifest.json").read_text())
    assert manifest["version"] == FAKE_VERSION
    marker = json.loads((override / MARKER_FILE).read_text())
    assert marker["pinned_version"] == FAKE_VERSION
    assert marker["core_at_pin"] == CORE_VERSION

    await client.send_json_auto_id({"type": f"{DOMAIN}/list"})
    snap = (await client.receive_json())["result"]
    assert snap["restart_required"] is True
    (pin,) = snap["pins"]
    assert pin["status"] == "pending"  # written to disk, but not loaded until restart
    assert pin["override"]["requirements"] == ["fakelib==1.0.0"]
    assert pin["override"]["core_requirements"] == []  # sun has none
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_OUT_OF_RANGE}_{FAKE_DOMAIN}") is None

    # The range only means anything once the override is the running code.
    _mark_loaded(hass, FAKE_DOMAIN, override)

    # --- update range to exclude the running core -> repair issue ----------
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/update", "domain": FAKE_DOMAIN, "core_range": "<2000.1.0"}
    )
    assert (await client.receive_json())["success"]
    await client.send_json_auto_id({"type": f"{DOMAIN}/list"})
    (pin,) = (await client.receive_json())["result"]["pins"]
    assert pin["status"] == "out_of_range"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_OUT_OF_RANGE}_{FAKE_DOMAIN}")
    assert issue is not None
    assert issue.severity == ir.IssueSeverity.WARNING
    # Warn-only: the override must still be in place.
    assert (override / "__init__.py").is_file()

    # --- widen the range again -> issue clears -----------------------------
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/update", "domain": FAKE_DOMAIN, "core_range": ""}
    )
    assert (await client.receive_json())["success"]
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_OUT_OF_RANGE}_{FAKE_DOMAIN}") is None

    # --- invalid range is rejected -----------------------------------------
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/update", "domain": FAKE_DOMAIN, "core_range": "banana"}
    )
    msg = await client.receive_json()
    assert not msg["success"] and msg["error"]["code"] == "pin_error"

    # --- persisted across reload ------------------------------------------
    from custom_components.integration_pins.store import PinStore

    store = PinStore(hass)
    await store.async_load()
    assert store.get(FAKE_DOMAIN).pinned_version == FAKE_VERSION

    # --- unpin -> directory retired, pin gone -------------------------------
    await client.send_json_auto_id({"type": f"{DOMAIN}/unpin", "domain": FAKE_DOMAIN})
    msg = await client.receive_json()
    assert msg["success"]
    # A non-null result is what makes the panel show its "restart to apply" notice.
    assert msg["result"] == {"domain": FAKE_DOMAIN, "override": "deleted"}
    assert not override.exists()
    assert not (tmp_path / RETIRED_DIRNAME).exists()  # pypi pins are re-downloadable
    await client.send_json_auto_id({"type": f"{DOMAIN}/list"})
    assert (await client.receive_json())["result"]["pins"] == []


async def test_pin_rejects_unknown_domain_and_missing_component(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, fake_wheel
):
    _mock_pypi(aioclient_mock, fake_wheel)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": "not_a_real_domain", "version": FAKE_VERSION}
    )
    msg = await client.receive_json()
    assert not msg["success"] and "not a core integration" in msg["error"]["message"]

    # 'hue' is a core domain but our fake wheel doesn't contain it.
    await client.send_json_auto_id({"type": f"{DOMAIN}/pin", "domain": "hue", "version": FAKE_VERSION})
    msg = await client.receive_json()
    assert not msg["success"] and "has no core integration 'hue'" in msg["error"]["message"]


async def test_bad_sha256_rejected(hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, fake_wheel, tmp_path):
    data = fake_wheel.read_bytes()
    aioclient_mock.get(
        f"https://pypi.org/pypi/homeassistant/{FAKE_VERSION}/json",
        json={"urls": [{"packagetype": "bdist_wheel", "url": "https://files.example/ha.whl", "digests": {"sha256": "0" * 64}, "size": len(data)}]},
    )
    aioclient_mock.get("https://files.example/ha.whl", content=data)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "version": FAKE_VERSION})
    msg = await client.receive_json()
    assert not msg["success"] and "sha256" in msg["error"]["message"]
    assert not (tmp_path / "custom_components" / FAKE_DOMAIN).exists()
    assert not list(tmp_path.glob(".integration_pins.*"))  # the failed download is cleaned up


async def test_a_release_pin_downloads_and_cleans_up_off_the_event_loop(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, fake_wheel, tmp_path
):
    """A wheel is 30-50 MB. Writing it, and removing the directory it was staged in, run in
    the executor; the autouse loop guard in conftest fails this test if either does not."""
    _mock_pypi(aioclient_mock, fake_wheel)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "version": FAKE_VERSION}
    )
    assert (await client.receive_json())["success"]

    assert (tmp_path / "custom_components" / FAKE_DOMAIN / "__init__.py").is_file()
    assert not list(tmp_path.glob(".integration_pins.*"))


async def test_adopt_and_unmanaged_listing(hass: HomeAssistant, setup, hass_ws_client, tmp_path):
    manual = tmp_path / "custom_components" / "hue"
    manual.mkdir(parents=True)
    (manual / "manifest.json").write_text(json.dumps({"domain": "hue", "version": "2025.11.0"}))
    # Adopt targets a directory Home Assistant already loaded at startup.
    _mark_loaded(hass, "hue", manual)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": f"{DOMAIN}/list"})
    snap = (await client.receive_json())["result"]
    assert [o["domain"] for o in snap["unmanaged_overrides"]] == ["hue"]
    # integration_pins itself must never show up as an override.
    assert all(o["domain"] != DOMAIN for o in snap["unmanaged_overrides"])

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/adopt", "domain": "hue", "pinned_version": "2025.11.0", "core_range": ""}
    )
    assert (await client.receive_json())["success"]
    await client.send_json_auto_id({"type": f"{DOMAIN}/list"})
    snap = (await client.receive_json())["result"]
    assert snap["unmanaged_overrides"] == []
    (pin,) = snap["pins"]
    assert pin["status"] == "active" and pin["source"] == "adopted"


async def test_versions_listing(hass: HomeAssistant, setup, hass_ws_client, aioclient_mock):
    aioclient_mock.get(
        "https://pypi.org/pypi/homeassistant/json",
        json={
            "releases": {
                "2026.2.3": [{"packagetype": "bdist_wheel"}],
                "2026.3.0b1": [{"packagetype": "bdist_wheel"}],
                "2026.1.0": [{"packagetype": "bdist_wheel", "yanked": True}],
                "2023.5.0": [{"packagetype": "bdist_wheel"}],
                "2025.12.3": [{"packagetype": "sdist"}, {"packagetype": "bdist_wheel"}],
            }
        },
    )
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/versions"})
    assert (await client.receive_json())["result"]["versions"] == ["2026.2.3", "2025.12.3"]
    await client.send_json_auto_id({"type": f"{DOMAIN}/versions", "include_prereleases": True})
    assert (await client.receive_json())["result"]["versions"] == ["2026.3.0b1", "2026.2.3", "2025.12.3"]


def test_range_semantics():
    assert pinner.version_in_range("2026.9.1", "==2026.9.1")
    assert pinner.version_in_range("2026.9.4", "~=2026.9.0")
    assert not pinner.version_in_range("2026.10.0", "~=2026.9.0")
    assert pinner.version_in_range("2026.10.0b2", ">=2026.9.0,<2026.11.0")
    assert pinner.version_in_range("2026.10.0", "")
    with pytest.raises(pinner.PinError):
        pinner.parse_range(">>2026")


async def test_core_domains_reports_loaded_components_as_in_use(
    hass: HomeAssistant, setup, hass_ws_client
):
    hass.config.components.add("hue")
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": f"{DOMAIN}/core_domains"})
    result = (await client.receive_json())["result"]

    assert "hue" in result["domains"] and "abode" in result["domains"]
    assert "hue" in result["in_use"]
    assert "abode" not in result["in_use"]


async def test_core_domains_counts_config_entry_domains_that_failed_to_load(
    hass: HomeAssistant, setup, hass_ws_client
):
    """A config entry whose setup failed never reaches hass.config.components."""
    MockConfigEntry(domain="abode", data={}).add_to_hass(hass)
    assert "abode" not in hass.config.components
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": f"{DOMAIN}/core_domains"})
    result = (await client.receive_json())["result"]

    assert "abode" in result["in_use"]


def _mark_loaded(hass: HomeAssistant, domain: str, path: Path) -> None:
    """Stand in for the startup scan that makes an override the running code."""
    from homeassistant import loader

    manifest = json.loads((path / "manifest.json").read_text())
    hass.data[loader.DATA_CUSTOM_COMPONENTS] = {
        domain: loader.Integration(hass, f"custom_components.{domain}", path, manifest)
    }


async def test_pin_is_pending_until_home_assistant_restarts(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, fake_wheel
):
    _mock_pypi(aioclient_mock, fake_wheel)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "version": FAKE_VERSION}
    )
    assert (await client.receive_json())["success"]

    await client.send_json_auto_id({"type": f"{DOMAIN}/list"})
    (pin,) = (await client.receive_json())["result"]["pins"]

    assert pin["status"] == "pending"


async def test_pin_becomes_active_once_the_override_is_the_running_code(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, fake_wheel, tmp_path
):
    _mock_pypi(aioclient_mock, fake_wheel)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "version": FAKE_VERSION}
    )
    assert (await client.receive_json())["success"]
    _mark_loaded(hass, FAKE_DOMAIN, tmp_path / "custom_components" / FAKE_DOMAIN)

    await client.send_json_auto_id({"type": f"{DOMAIN}/list"})
    (pin,) = (await client.receive_json())["result"]["pins"]

    assert pin["status"] == "active"


async def test_repinning_to_another_release_is_pending_again(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, fake_wheel, tmp_path
):
    _mock_pypi(aioclient_mock, fake_wheel)
    newer = "2026.1.0"
    _mock_pypi(aioclient_mock, build_fake_wheel(tmp_path, version=newer), version=newer)
    override = tmp_path / "custom_components" / FAKE_DOMAIN
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "version": FAKE_VERSION}
    )
    assert (await client.receive_json())["success"]
    _mark_loaded(hass, FAKE_DOMAIN, override)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "version": newer}
    )
    assert (await client.receive_json())["success"], "re-pin should succeed"

    await client.send_json_auto_id({"type": f"{DOMAIN}/list"})
    (pin,) = (await client.receive_json())["result"]["pins"]

    assert pin["pinned_version"] == newer
    assert pin["status"] == "pending"  # the loaded code is still the old release


async def test_domain_info_reports_integrations_that_depend_on_the_domain(
    hass: HomeAssistant, setup, hass_ws_client
):
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": f"{DOMAIN}/domain_info", "domain": "bluetooth"})
    result = (await client.receive_json())["result"]

    assert "esphome" in result["dependents"]  # declares bluetooth directly
    assert "acaia" in result["dependents"]  # only via bluetooth_adapters
    assert result["loaded_dependents"] == []


async def test_domain_info_singles_out_dependents_loaded_here(
    hass: HomeAssistant, setup, hass_ws_client
):
    hass.config.components.add("acaia")
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": f"{DOMAIN}/domain_info", "domain": "bluetooth"})
    result = (await client.receive_json())["result"]

    assert result["loaded_dependents"] == ["acaia"]


async def test_domain_info_for_a_leaf_integration_has_no_dependents(
    hass: HomeAssistant, setup, hass_ws_client
):
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": f"{DOMAIN}/domain_info", "domain": "hue"})
    result = (await client.receive_json())["result"]

    assert result["dependents"] == []


def _write_custom_integration(root: Path, domain: str, **manifest) -> Path:
    path = root / "custom_components" / domain
    path.mkdir(parents=True, exist_ok=True)
    (path / "manifest.json").write_text(
        json.dumps({"domain": domain, "name": domain.title(), **manifest})
    )
    return path


async def _load_custom(hass: HomeAssistant, *paths: Path) -> None:
    """Add directories to what the loader found in custom_components at startup."""
    from homeassistant import loader

    found = dict(await loader.async_get_custom_components(hass))
    for path in paths:
        manifest = json.loads((path / "manifest.json").read_text())
        found[path.name] = loader.Integration(
            hass, f"custom_components.{path.name}", path, manifest
        )
    hass.data[loader.DATA_CUSTOM_COMPONENTS] = found


def _spy_on_file_access(monkeypatch) -> list[str]:
    touched: list[str] = []
    for module, name in ((builtins, "open"), (io, "open"), (os, "open"), (os, "stat")):
        original = getattr(module, name)

        def spy(path, *args, _original=original, **kwargs):
            touched.append(str(path))
            return _original(path, *args, **kwargs)

        monkeypatch.setattr(module, name, spy)
    return touched


async def test_snapshot_lists_third_party_custom_integrations_from_the_loader(
    hass: HomeAssistant, setup, hass_ws_client, tmp_path, monkeypatch
):
    """What Home Assistant loaded, as it loaded it -- not read out of their own files."""
    my_hack = _write_custom_integration(tmp_path, "my_hack", name="My Hack", version="0.1.0")
    _write_custom_integration(tmp_path, "late_arrival", version="1.0.0")  # after startup
    await _load_custom(hass, my_hack)
    touched = _spy_on_file_access(monkeypatch)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": f"{DOMAIN}/list"})
    snap = (await client.receive_json())["result"]

    assert snap["custom_integrations"] == [
        {"domain": "my_hack", "name": "My Hack", "version": "0.1.0"}
    ]
    assert not [p for p in touched if "my_hack" in Path(p).parent.parts]  # nothing inside it


def _write_hacs_storage(root: Path, domain: str, repo: str) -> Path:
    storage = root / ".storage"
    storage.mkdir(exist_ok=True)
    path = storage / "hacs.data"
    path.write_text(
        json.dumps({"data": {"repositories": {"1": {"full_name": repo, "domain": domain}}}})
    )
    return path


async def test_another_integrations_storage_is_never_read(
    hass: HomeAssistant, setup, hass_ws_client, tmp_path, monkeypatch
):
    """.storage belongs to the integrations that wrote it, HACS's included."""
    alarmo = _write_custom_integration(tmp_path, "alarmo", version="1.10.5")
    _write_custom_integration(tmp_path, "plant", version="2.0.0")  # shadows a core domain
    _write_hacs_storage(tmp_path, "alarmo", "nielsfaber/alarmo")
    await _load_custom(hass, alarmo)
    touched = _spy_on_file_access(monkeypatch)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": f"{DOMAIN}/list"})
    snap = (await client.receive_json())["result"]

    assert snap["custom_integrations"] == [
        {"domain": "alarmo", "name": "Alarmo", "version": "1.10.5"}
    ]
    (override,) = snap["unmanaged_overrides"]
    assert override["domain"] == "plant" and "source" not in override
    assert not [p for p in touched if ".storage" in Path(p).parts]


async def test_custom_integration_listing_excludes_pins_and_core_shadows(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, fake_wheel, tmp_path
):
    _mock_pypi(aioclient_mock, fake_wheel)
    hue = _write_custom_integration(tmp_path, "hue", version="2025.11.0")  # shadows a core domain
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "version": FAKE_VERSION}
    )
    assert (await client.receive_json())["success"]
    await _load_custom(hass, hue, tmp_path / "custom_components" / FAKE_DOMAIN)

    await client.send_json_auto_id({"type": f"{DOMAIN}/list"})
    snap = (await client.receive_json())["result"]

    listed = {e["domain"] for e in snap["custom_integrations"]}
    assert listed == set()  # hue is a core shadow, sun is pinned, integration_pins is us
    assert [o["domain"] for o in snap["unmanaged_overrides"]] == ["hue"]


async def test_unpinning_a_pypi_pin_deletes_it_rather_than_retiring(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, fake_wheel, tmp_path
):
    """A pypi pin records domain + release, so the files can always be fetched again."""
    _mock_pypi(aioclient_mock, fake_wheel)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "version": FAKE_VERSION}
    )
    assert (await client.receive_json())["success"]

    await client.send_json_auto_id({"type": f"{DOMAIN}/unpin", "domain": FAKE_DOMAIN})
    assert (await client.receive_json())["result"]["override"] == "deleted"

    assert not (tmp_path / "custom_components" / FAKE_DOMAIN).exists()
    assert not (tmp_path / RETIRED_DIRNAME).exists()


async def test_unpinning_an_adopted_pin_retires_it(
    hass: HomeAssistant, setup, hass_ws_client, tmp_path
):
    """An adopted directory was placed by hand and exists nowhere else."""
    manual = _write_custom_integration(tmp_path, "hue", version="2025.11.0")
    _mark_loaded(hass, "hue", manual)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/adopt", "domain": "hue", "pinned_version": "2025.11.0"}
    )
    assert (await client.receive_json())["success"]

    await client.send_json_auto_id({"type": f"{DOMAIN}/unpin", "domain": "hue"})
    assert (await client.receive_json())["result"]["override"] == "retired"

    retired = list((tmp_path / RETIRED_DIRNAME).iterdir())
    assert len(retired) == 1 and retired[0].name.startswith("hue-2025.11.0-")


def _replace_override(override: Path, how: str) -> None:
    """What HACS, a manual copy or another tool leaves behind in a pinned directory."""
    (override / "__init__.py").write_text("someone else's code\n")
    if how == "no_marker":
        (override / MARKER_FILE).unlink()
    else:
        marker = json.loads((override / MARKER_FILE).read_text())
        (override / MARKER_FILE).write_text(json.dumps({**marker, "pinned_version": "2024.1.0"}))


@pytest.mark.parametrize("how", ["no_marker", "other_marker"])
async def test_unpinning_a_replaced_pin_leaves_the_directory_alone(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, fake_wheel, tmp_path, how
):
    """Only the code this pin wrote is ours to delete; anything else loses just the pin."""
    _mock_pypi(aioclient_mock, fake_wheel)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "version": FAKE_VERSION}
    )
    assert (await client.receive_json())["success"]
    override = tmp_path / "custom_components" / FAKE_DOMAIN
    _replace_override(override, how)
    before = {p.relative_to(override): p.read_bytes() for p in override.rglob("*") if p.is_file()}
    await client.send_json_auto_id({"type": f"{DOMAIN}/list"})
    (pin,) = (await client.receive_json())["result"]["pins"]
    assert pin["status"] == "foreign"

    await client.send_json_auto_id({"type": f"{DOMAIN}/unpin", "domain": FAKE_DOMAIN})
    msg = await client.receive_json()

    assert msg["result"] == {"domain": FAKE_DOMAIN, "override": "left"}
    after = {p.relative_to(override): p.read_bytes() for p in override.rglob("*") if p.is_file()}
    assert after == before
    assert not (tmp_path / RETIRED_DIRNAME).exists()
    await client.send_json_auto_id({"type": f"{DOMAIN}/list"})
    snap = (await client.receive_json())["result"]
    assert snap["pins"] == []
    assert snap["restart_required"] is False  # nothing on disk changed


async def test_unpinning_a_replaced_adopted_pin_does_not_retire_it(
    hass: HomeAssistant, setup, hass_ws_client, tmp_path
):
    manual = _write_custom_integration(tmp_path, "hue", version="2025.11.0")
    _mark_loaded(hass, "hue", manual)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/adopt", "domain": "hue", "pinned_version": "2025.11.0"}
    )
    assert (await client.receive_json())["success"]
    _replace_override(manual, "no_marker")

    await client.send_json_auto_id({"type": f"{DOMAIN}/unpin", "domain": "hue"})
    assert (await client.receive_json())["result"]["override"] == "left"

    assert (manual / "__init__.py").read_text() == "someone else's code\n"
    assert not (tmp_path / RETIRED_DIRNAME).exists()


def test_remove_override_checks_the_marker_itself(tmp_path, fake_wheel):
    """The guard sits next to the rmtree, not only in the caller."""
    override = pinner.extract_component(
        fake_wheel, FAKE_DOMAIN, FAKE_VERSION, str(tmp_path), CORE_VERSION
    )

    assert pinner.remove_override(FAKE_DOMAIN, "2024.1.0", str(tmp_path), retire=False) == (
        "left",
        None,
    )
    assert (override / "__init__.py").is_file()

    assert pinner.remove_override(FAKE_DOMAIN, FAKE_VERSION, str(tmp_path), retire=False) == (
        "deleted",
        None,
    )
    assert not override.exists()
    assert pinner.remove_override(FAKE_DOMAIN, FAKE_VERSION, str(tmp_path)) == ("absent", None)


async def test_snapshot_lists_retired_directories_with_sizes(
    hass: HomeAssistant, setup, hass_ws_client, tmp_path
):
    retired = tmp_path / RETIRED_DIRNAME / "hue-2025.11.0-20260101T000000Z"
    retired.mkdir(parents=True)
    (retired / "__init__.py").write_bytes(b"x" * 1234)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": f"{DOMAIN}/list"})
    (entry,) = (await client.receive_json())["result"]["retired"]

    assert entry["name"] == "hue-2025.11.0-20260101T000000Z"
    assert entry["bytes"] == 1234


async def test_delete_retired_removes_one_entry(
    hass: HomeAssistant, setup, hass_ws_client, tmp_path
):
    root = tmp_path / RETIRED_DIRNAME
    (root / "hue-a").mkdir(parents=True)
    (root / "zha-b").mkdir(parents=True)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": f"{DOMAIN}/delete_retired", "name": "hue-a"})
    assert (await client.receive_json())["success"]

    assert [p.name for p in root.iterdir()] == ["zha-b"]


async def test_delete_retired_refuses_a_path_outside_the_retired_folder(
    hass: HomeAssistant, setup, hass_ws_client, tmp_path
):
    victim = tmp_path / "custom_components" / "hue"
    victim.mkdir(parents=True)
    (tmp_path / RETIRED_DIRNAME).mkdir()
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/delete_retired", "name": "../custom_components/hue"}
    )
    msg = await client.receive_json()

    assert msg["error"]["code"] == "pin_error"
    assert "not a retired override" in msg["error"]["message"]
    assert victim.exists()


async def test_clear_retired_empties_the_folder(
    hass: HomeAssistant, setup, hass_ws_client, tmp_path
):
    root = tmp_path / RETIRED_DIRNAME
    (root / "hue-a").mkdir(parents=True)
    (root / "zha-b").mkdir(parents=True)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": f"{DOMAIN}/clear_retired"})
    assert (await client.receive_json())["result"] == {"removed": 2}

    assert list(root.iterdir()) == []


async def test_clear_retired_leaves_files_it_did_not_put_there(
    hass: HomeAssistant, setup, hass_ws_client, tmp_path
):
    """Only directories are retired copies, and only those are listed to be cleared."""
    root = tmp_path / RETIRED_DIRNAME
    (root / "hue-a").mkdir(parents=True)
    (root / "notes.txt").write_text("mine")
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": f"{DOMAIN}/clear_retired"})
    assert (await client.receive_json())["result"] == {"removed": 1}

    assert [p.name for p in root.iterdir()] == ["notes.txt"]


def test_parse_central_directory_matches_the_zip_it_came_from(tmp_path):
    import zipfile

    wheel = build_fake_wheel(tmp_path)
    with zipfile.ZipFile(wheel) as zf:
        expected = {i.filename: (i.CRC, i.file_size) for i in zf.infolist()}

    entries = pinner.parse_central_directory(wheel.read_bytes())

    assert {name: (e.crc, e.size) for name, e in entries.items()} == expected


def test_local_component_digest_skips_bytecode(tmp_path):
    import zlib

    src = tmp_path / "sun"
    (src / "__pycache__").mkdir(parents=True)
    (src / "__init__.py").write_bytes(b"hello")
    (src / "__pycache__" / "x.pyc").write_bytes(b"junk")

    digest = pinner.local_component_digest(src)

    assert digest == {"__init__.py": (zlib.crc32(b"hello"), 5)}


def test_compare_digests_splits_added_removed_and_changed():
    base = {"a.py": (1, 10), "gone.py": (2, 20), "same.py": (3, 30)}
    candidate = {"a.py": (9, 11), "new.py": (4, 40), "same.py": (3, 30)}

    result = pinner.compare_digests(base, candidate)

    assert result == {
        "added": ["new.py"],
        "removed": ["gone.py"],
        "changed": ["a.py"],
        "unchanged": 1,
    }


async def test_compare_reports_differences_against_the_running_core(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, fake_wheel
):
    _mock_pypi(aioclient_mock, fake_wheel)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/compare", "domain": FAKE_DOMAIN, "version": FAKE_VERSION}
    )
    result = (await client.receive_json())["result"]

    assert result["identical"] is False
    assert result["compared_with"] == f"core {CORE_VERSION}"
    assert "manifest.json" in result["changed"]
    # Bytecode is not part of either side.
    assert not any("__pycache__" in name for name in result["added"] + result["removed"])


async def test_compare_uses_the_pinned_release_as_the_baseline(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, fake_wheel, tmp_path
):
    """The on-disk override carries a marker and an injected manifest version, so the
    honest baseline for an existing pypi pin is the release it came from."""
    newer = "2026.1.0"
    _mock_pypi(aioclient_mock, fake_wheel)
    _mock_pypi(aioclient_mock, build_fake_wheel(tmp_path, version=newer), version=newer)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "version": FAKE_VERSION}
    )
    assert (await client.receive_json())["success"]

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/compare", "domain": FAKE_DOMAIN, "version": newer}
    )
    result = (await client.receive_json())["result"]

    assert result["compared_with"] == f"pinned {FAKE_VERSION}"
    assert result["changed"] == ["__init__.py"]  # only the docstring names the release
    assert result["added"] == [] and result["removed"] == []


async def test_compare_rejects_a_release_without_that_integration(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, fake_wheel
):
    _mock_pypi(aioclient_mock, fake_wheel)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/compare", "domain": "hue", "version": FAKE_VERSION}
    )
    msg = await client.receive_json()

    assert not msg["success"] and "has no core integration 'hue'" in msg["error"]["message"]


def test_module_url_changes_when_the_panel_file_changes(tmp_path):
    from custom_components.integration_pins import _module_url
    from custom_components.integration_pins.const import PANEL_JS_FILE, PANEL_STATIC_URL

    panel = tmp_path / PANEL_JS_FILE
    panel.write_text("console.log(1)")
    first = _module_url("0.1.0", panel)
    panel.write_text("console.log(2)")
    second = _module_url("0.1.0", panel)

    assert first.startswith(f"{PANEL_STATIC_URL}/{PANEL_JS_FILE}?v=0.1.0-")
    assert first != second


async def test_panel_module_is_served_with_revalidation(hass: HomeAssistant, setup, hass_client):
    """No Cache-Control at all leaves browsers free to serve a stale panel."""
    from custom_components.integration_pins.const import PANEL_JS_FILE, PANEL_STATIC_URL

    client = await hass_client()

    resp = await client.get(f"{PANEL_STATIC_URL}/{PANEL_JS_FILE}")

    assert resp.status == 200
    assert resp.headers["Cache-Control"] == "no-cache"
    assert "IntegrationPinsPanel" in await resp.text()


async def test_unchanged_panel_revalidates_to_an_empty_304(
    hass: HomeAssistant, setup, hass_client
):
    """no-cache costs a round trip, not a re-download."""
    from custom_components.integration_pins.const import PANEL_JS_FILE, PANEL_STATIC_URL

    client = await hass_client()
    url = f"{PANEL_STATIC_URL}/{PANEL_JS_FILE}"
    first = await client.get(url)
    validator = first.headers.get("ETag") or first.headers.get("Last-Modified")
    assert validator, "nothing for the browser to revalidate against"
    header = "If-None-Match" if first.headers.get("ETag") else "If-Modified-Since"

    second = await client.get(url, headers={header: validator})

    assert second.status == 304
    assert await second.read() == b""


CORE_REPO = "home-assistant/core"
FORK = "someone/core"


def _gh_api(repo=CORE_REPO):
    return f"https://api.github.com/repos/{repo}"


def _gh_raw(repo=CORE_REPO):
    return f"https://raw.githubusercontent.com/{repo}"


GH_API = _gh_api()
GH_RAW = _gh_raw()
FAKE_SHA = "90258e6ef6ca10d9789bd92004805143ca9b6cf0"
FAKE_CONST_PY = (
    "MAJOR_VERSION: Final = 2026\n"
    "MINOR_VERSION: Final = 10\n"
    'PATCH_VERSION: Final = "0.dev0"\n'
    '__version__: Final = f"{__short_version__}.{PATCH_VERSION}"\n'
)


def _mock_github_ref(aioclient_mock, ref="dev", sha=FAKE_SHA, date="", repo=CORE_REPO):
    aioclient_mock.get(
        f"{_gh_api(repo)}/commits/{ref}",
        json={"sha": sha, "commit": {"committer": {"date": date}}},
    )
    aioclient_mock.get(f"{_gh_raw(repo)}/{sha}/homeassistant/const.py", text=FAKE_CONST_PY)


async def test_resolving_a_git_ref_records_the_commit_and_the_core_it_targets(
    hass: HomeAssistant, setup, aioclient_mock
):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    _mock_github_ref(aioclient_mock)

    source = await pinner.async_resolve_git_source(async_get_clientsession(hass), "dev")

    assert source.ref == "dev"
    assert source.sha == FAKE_SHA
    assert source.core_version == "2026.10.0.dev0"
    assert source.version == "2026.10.0.dev0+90258e6"


async def test_resolving_an_unknown_git_ref_is_a_clean_error(
    hass: HomeAssistant, setup, aioclient_mock
):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    aioclient_mock.get(f"{GH_API}/commits/nope", status=404, json={"message": "Not Found"})

    with pytest.raises(pinner.PinError, match="nope"):
        await pinner.async_resolve_git_source(async_get_clientsession(hass), "nope")


def _mock_github_tree(aioclient_mock, domain, files, sha=FAKE_SHA, extra_tree=(), repo=CORE_REPO):
    tree = [{"path": p, "type": "blob", "size": len(b)} for p, b in files.items()]
    tree.extend(extra_tree)
    aioclient_mock.get(
        f"{_gh_api(repo)}/git/trees/{sha}:homeassistant/components/{domain}",
        json={"tree": tree, "truncated": False},
    )
    for path, data in files.items():
        aioclient_mock.get(
            f"{_gh_raw(repo)}/{sha}/homeassistant/components/{domain}/{path}", content=data
        )


def _git_source():
    return pinner.GitSource(ref="dev", sha=FAKE_SHA, core_version="2026.10.0.dev0")


async def test_git_component_fetches_every_blob_in_the_tree(
    hass: HomeAssistant, setup, aioclient_mock
):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    files = {"__init__.py": b"code", "manifest.json": b"{}", "translations/en.json": b"{}"}
    _mock_github_tree(
        aioclient_mock,
        "sun",
        files,
        extra_tree=[
            {"path": "translations", "type": "tree"},
            {"path": "__pycache__/x.pyc", "type": "blob", "size": 4},
        ],
    )

    got = await pinner.async_git_component(async_get_clientsession(hass), _git_source(), "sun")

    assert got == files


async def test_git_component_rejects_a_commit_without_that_integration(
    hass: HomeAssistant, setup, aioclient_mock
):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    aioclient_mock.get(
        f"{GH_API}/git/trees/{FAKE_SHA}:homeassistant/components/nope",
        status=404,
        json={"message": "Not Found"},
    )

    with pytest.raises(pinner.PinError, match="no core integration 'nope'"):
        await pinner.async_git_component(async_get_clientsession(hass), _git_source(), "nope")


def test_install_component_writes_the_files_manifest_version_and_marker(tmp_path):
    files = {
        "__init__.py": b"code",
        "manifest.json": b'{"domain": "sun", "requirements": []}',
        "translations/en.json": b"{}",
    }

    target = pinner.install_component(
        files,
        "sun",
        "2026.10.0.dev0+90258e6",
        str(tmp_path),
        CORE_VERSION,
        {"git_ref": "dev", "git_sha": FAKE_SHA},
    )

    assert alias.strip((target / "__init__.py").read_bytes()) == b"code"
    assert (target / alias.SHIM_FILE).is_file()
    assert (target / "translations" / "en.json").is_file()
    assert json.loads((target / "manifest.json").read_text())["version"] == "2026.10.0.dev0+90258e6"
    marker = json.loads((target / MARKER_FILE).read_text())
    assert marker["git_ref"] == "dev" and marker["git_sha"] == FAKE_SHA


def test_install_component_refuses_a_path_that_escapes_the_target(tmp_path):
    files = {"manifest.json": b"{}", "../../evil.py": b"pwned"}

    with pytest.raises(pinner.PinError, match="outside"):
        pinner.install_component(files, "sun", "1.0.0", str(tmp_path), CORE_VERSION)

    assert not (tmp_path.parent / "evil.py").exists()
    assert not (tmp_path / "custom_components" / "sun").exists()


async def test_pinning_from_git_records_the_commit_and_the_core_it_targets(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, tmp_path
):
    _mock_github_ref(aioclient_mock)
    _mock_github_tree(
        aioclient_mock,
        FAKE_DOMAIN,
        {
            "__init__.py": b"the fix that just landed",
            "manifest.json": b'{"domain": "sun", "requirements": []}',
        },
    )
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "git_ref": "dev", "core_range": ""}
    )
    assert (await client.receive_json())["success"]

    override = tmp_path / "custom_components" / FAKE_DOMAIN
    assert alias.strip((override / "__init__.py").read_bytes()) == b"the fix that just landed"
    assert json.loads((override / "manifest.json").read_text())["version"] == (
        "2026.10.0.dev0+90258e6"
    )

    await client.send_json_auto_id({"type": f"{DOMAIN}/list"})
    (pin,) = (await client.receive_json())["result"]["pins"]
    assert pin["source"] == "git"
    assert pin["pinned_version"] == "2026.10.0.dev0+90258e6"
    assert pin["git_ref"] == "dev"
    assert pin["git_sha"] == FAKE_SHA
    assert pin["status"] == "pending"


async def test_repinning_a_git_pin_at_its_commit_keeps_the_ref_it_was_asked_for(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock
):
    files = {"__init__.py": b"x", "manifest.json": b'{"domain": "sun", "requirements": []}'}
    _mock_github_ref(aioclient_mock)
    _mock_github_ref(aioclient_mock, ref=FAKE_SHA)
    _mock_github_tree(aioclient_mock, FAKE_DOMAIN, files)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "git_ref": "dev", "core_range": ""}
    )
    assert (await client.receive_json())["success"]
    # The panel's Re-pin sends the recorded commit, so a moved branch is not picked up.
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "git_ref": FAKE_SHA, "core_range": ""}
    )
    assert (await client.receive_json())["success"]

    await client.send_json_auto_id({"type": f"{DOMAIN}/list"})
    (pin,) = (await client.receive_json())["result"]["pins"]
    assert pin["git_ref"] == "dev"
    assert pin["git_sha"] == FAKE_SHA


async def test_pin_needs_exactly_one_of_release_or_git_ref(
    hass: HomeAssistant, setup, hass_ws_client
):
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN})
    neither = await client.receive_json()
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "version": "2025.1.0", "git_ref": "dev"}
    )
    both = await client.receive_json()

    for msg in (neither, both):
        assert not msg["success"] and msg["error"]["code"] == "pin_error"


OTHER_SHA = "1111111222222233333334444444555555566666"


async def test_compare_works_against_a_git_ref(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock
):
    _mock_github_ref(aioclient_mock)
    _mock_github_tree(
        aioclient_mock,
        FAKE_DOMAIN,
        {"__init__.py": b"new", "manifest.json": b'{"domain": "sun"}'},
    )
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/compare", "domain": FAKE_DOMAIN, "git_ref": "dev"}
    )
    result = (await client.receive_json())["result"]

    assert result["identical"] is False
    assert result["compared_with"] == f"core {CORE_VERSION}"
    assert "manifest.json" in result["changed"]


async def test_compare_baselines_a_git_pin_against_the_commit_it_came_from(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock
):
    pinned = {"__init__.py": b"old", "manifest.json": b'{"domain": "sun"}'}
    _mock_github_ref(aioclient_mock)
    _mock_github_tree(aioclient_mock, FAKE_DOMAIN, pinned)
    _mock_github_ref(aioclient_mock, ref="fix", sha=OTHER_SHA)
    _mock_github_tree(
        aioclient_mock,
        FAKE_DOMAIN,
        {"__init__.py": b"new", "manifest.json": b'{"domain": "sun"}'},
        sha=OTHER_SHA,
    )
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "git_ref": "dev"}
    )
    assert (await client.receive_json())["success"]

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/compare", "domain": FAKE_DOMAIN, "git_ref": "fix"}
    )
    result = (await client.receive_json())["result"]

    assert result["compared_with"] == "pinned 2026.10.0.dev0+90258e6"
    assert result["changed"] == ["__init__.py"]
    assert result["added"] == [] and result["removed"] == []


async def test_unpinning_a_git_pin_deletes_it_rather_than_retiring(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, tmp_path
):
    """A git pin records the commit, so its files are reproducible too."""
    _mock_github_ref(aioclient_mock)
    _mock_github_tree(
        aioclient_mock, FAKE_DOMAIN, {"__init__.py": b"x", "manifest.json": b'{"domain": "sun"}'}
    )
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "git_ref": "dev"}
    )
    assert (await client.receive_json())["success"]

    await client.send_json_auto_id({"type": f"{DOMAIN}/unpin", "domain": FAKE_DOMAIN})
    assert (await client.receive_json())["result"]["override"] == "deleted"

    assert not (tmp_path / "custom_components" / FAKE_DOMAIN).exists()
    assert not (tmp_path / RETIRED_DIRNAME).exists()


async def test_snapshot_advertises_what_this_backend_can_do(
    hass: HomeAssistant, setup, hass_ws_client
):
    """The panel updates on a browser reload; Python only on a restart. The panel needs
    to be able to tell it is talking to an older backend."""
    from custom_components.integration_pins.const import FEATURES

    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": f"{DOMAIN}/list"})
    snap = (await client.receive_json())["result"]

    assert snap["features"] == list(FEATURES)
    assert "git" in snap["features"]
    assert "git_repo" in snap["features"]
    assert "upload" in snap["features"]
    assert "confirm_replace_foreign" in snap["features"]
    assert "unpin_keeps_replaced" in snap["features"]


async def test_version_listing_carries_release_dates(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock
):
    """The dates come free with the version list, and turn into 'what changed since' links."""
    aioclient_mock.get(
        "https://pypi.org/pypi/homeassistant/json",
        json={
            "releases": {
                "2026.2.3": [
                    {
                        "packagetype": "bdist_wheel",
                        "upload_time_iso_8601": "2026-02-20T21:03:06.197770Z",
                    }
                ],
                "2025.12.3": [{"packagetype": "bdist_wheel"}],
            }
        },
    )
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": f"{DOMAIN}/versions"})
    result = (await client.receive_json())["result"]

    assert result["versions"] == ["2026.2.3", "2025.12.3"]
    assert result["released"]["2026.2.3"] == "2026-02-20"
    assert result["released"]["2025.12.3"] == ""


async def test_compare_reports_when_the_candidate_shipped(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, fake_wheel
):
    _mock_pypi(aioclient_mock, fake_wheel, released="2025-12-09T11:00:00.000000Z")
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/compare", "domain": FAKE_DOMAIN, "version": FAKE_VERSION}
    )
    result = (await client.receive_json())["result"]

    assert result["since"] == "2025-12-09"


async def test_compare_against_git_reports_the_commit_date(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock
):
    _mock_github_ref(aioclient_mock, date="2026-09-08T10:11:12Z")
    _mock_github_tree(aioclient_mock, FAKE_DOMAIN, {"manifest.json": b'{"domain": "sun"}'})
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/compare", "domain": FAKE_DOMAIN, "git_ref": "dev"}
    )
    result = (await client.receive_json())["result"]

    assert result["since"] == "2026-09-08"


def test_config_entry_schema_defaults_to_one_when_undeclared():
    """Home Assistant's ConfigFlow defaults both to 1, so an absent value is knowable."""
    assert pinner.config_entry_schema("class Flow(ConfigFlow, domain=DOMAIN):\n    pass\n") == (1, 1)


def test_config_entry_schema_reads_the_declared_versions():
    source = (
        "class EcobeeFlow(ConfigFlow, domain=DOMAIN):\n"
        "    VERSION = 3\n"
        "    MINOR_VERSION = 2\n"
        "\n"
        "class EcobeeOptionsFlow(OptionsFlow):\n"
        "    pass\n"
    )

    assert pinner.config_entry_schema(source) == (3, 2)


def test_migration_markers_find_config_entry_and_unique_id_migrations():
    files = {
        "__init__.py": b"async def async_migrate_entry(hass, entry) -> bool:\n    return True\n",
        "sensor.py": b"await er.async_migrate_entries(hass, entry.entry_id, _migrate)\n",
    }

    assert pinner.migration_markers(files) == {"config_entry": True, "unique_id": True}


def test_migration_markers_ignore_ordinary_registry_writes():
    """async_update_entity is used for renames and device classes far more than migrations."""
    files = {"sensor.py": b"registry.async_update_entity(entity_id, name='New name')\n"}

    assert pinner.migration_markers(files) == {"config_entry": False, "unique_id": False}


async def test_reading_named_files_out_of_a_wheel_without_downloading_it(
    hass: HomeAssistant, setup, aioclient_mock, fake_wheel
):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    _mock_pypi(aioclient_mock, fake_wheel)
    session = async_get_clientsession(hass)
    wheel = await pinner.async_get_wheel(session, FAKE_VERSION)

    files = await pinner.async_read_wheel_files(
        session, wheel, FAKE_DOMAIN, ["manifest.json", "__init__.py", "not_there.py"]
    )

    assert json.loads(files["manifest.json"])["domain"] == FAKE_DOMAIN
    assert files["__init__.py"] == f'"""fake {FAKE_DOMAIN} from {FAKE_VERSION}"""\n'.encode()
    assert "not_there.py" not in files


CONFIG_FLOW = "class SunFlow(ConfigFlow, domain=DOMAIN):\n    VERSION = {major}\n"
MIGRATE_HANDLER = "async def async_migrate_entry(hass, entry) -> bool:\n    return True\n"


def _git_code(major, *, handler=False, unique_id=False):
    init = MIGRATE_HANDLER if handler else "async def async_setup_entry(hass, entry):\n    ...\n"
    if unique_id:
        init += "await er.async_migrate_entries(hass, entry.entry_id, _migrate)\n"
    return {
        "manifest.json": b'{"domain": "sun", "requirements": []}',
        "config_flow.py": CONFIG_FLOW.format(major=major).encode(),
        "__init__.py": init.encode(),
    }


async def _compare_git(hass_ws_client, hass):
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/compare", "domain": FAKE_DOMAIN, "git_ref": "dev"}
    )
    return (await client.receive_json())["result"]["migration"]


async def test_compare_reports_a_pin_that_cannot_set_up(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock
):
    """Older code with no migration handler cannot read an entry a newer one wrote."""
    MockConfigEntry(domain=FAKE_DOMAIN, version=3, minor_version=1).add_to_hass(hass)
    _mock_github_ref(aioclient_mock)
    _mock_github_tree(aioclient_mock, FAKE_DOMAIN, _git_code(2))

    migration = await _compare_git(hass_ws_client, hass)

    assert migration["verdict"] == "blocked"
    assert migration["stored"] == [3, 1] and migration["code"] == [2, 1]


async def test_compare_flags_a_migration_that_unpinning_will_not_undo(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock
):
    """Newer code migrates the stored entry for good; the bundled code then cannot read it."""
    MockConfigEntry(domain=FAKE_DOMAIN, version=1, minor_version=1).add_to_hass(hass)
    _mock_github_ref(aioclient_mock)
    _mock_github_tree(aioclient_mock, FAKE_DOMAIN, _git_code(2, handler=True))

    migration = await _compare_git(hass_ws_client, hass)

    assert migration["verdict"] == "forward"


async def test_compare_flags_a_unique_id_migration_in_the_code(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock
):
    MockConfigEntry(domain=FAKE_DOMAIN, version=1, minor_version=1).add_to_hass(hass)
    _mock_github_ref(aioclient_mock)
    _mock_github_tree(aioclient_mock, FAKE_DOMAIN, _git_code(1, unique_id=True))

    migration = await _compare_git(hass_ws_client, hass)

    assert migration["verdict"] == "same"
    assert migration["unique_id_migration"] is True


async def test_compare_says_nothing_to_migrate_when_the_integration_is_not_configured(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock
):
    _mock_github_ref(aioclient_mock)
    _mock_github_tree(aioclient_mock, FAKE_DOMAIN, _git_code(3))

    migration = await _compare_git(hass_ws_client, hass)

    assert migration["verdict"] == "no_entry"


async def test_pin_refuses_a_blocked_migration_until_it_is_acknowledged(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, tmp_path
):
    MockConfigEntry(domain=FAKE_DOMAIN, version=3, minor_version=1).add_to_hass(hass)
    _mock_github_ref(aioclient_mock)
    _mock_github_tree(aioclient_mock, FAKE_DOMAIN, _git_code(2))
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "git_ref": "dev"}
    )
    refused = await client.receive_json()

    # Its own code, so the panel can offer the tick without matching on message text.
    assert not refused["success"] and refused["error"]["code"] == "migration_blocked"
    assert not (tmp_path / "custom_components" / FAKE_DOMAIN).exists()

    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/pin",
            "domain": FAKE_DOMAIN,
            "git_ref": "dev",
            "acknowledge_migration": True,
        }
    )
    assert (await client.receive_json())["success"]
    assert (tmp_path / "custom_components" / FAKE_DOMAIN).is_dir()


async def test_pin_from_a_release_checks_the_migration_before_downloading(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, tmp_path
):
    MockConfigEntry(domain=FAKE_DOMAIN, version=3, minor_version=1).add_to_hass(hass)
    wheel = build_fake_wheel(tmp_path, extra={"config_flow.py": CONFIG_FLOW.format(major=2)})
    _mock_pypi(aioclient_mock, wheel)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "version": FAKE_VERSION}
    )
    msg = await client.receive_json()

    assert not msg["success"] and "config entry" in msg["error"]["message"]


# -- forks -----------------------------------------------------------------------


async def test_resolving_a_ref_in_a_fork_reads_the_fork(
    hass: HomeAssistant, setup, aioclient_mock
):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    _mock_github_ref(aioclient_mock, repo=FORK)

    source = await pinner.async_resolve_git_source(async_get_clientsession(hass), "dev", FORK)

    assert source.repo == FORK
    assert source.sha == FAKE_SHA
    assert source.version == "2026.10.0.dev0+90258e6"


@pytest.mark.parametrize(
    "repo", ["core", "a/b/c", "someone/..", "owner name/core", "https://example.com/a/b"]
)
def test_a_malformed_repository_is_refused(repo):
    with pytest.raises(pinner.PinError, match="owner/name"):
        pinner.normalize_repo(repo)


@pytest.mark.parametrize(
    "pasted",
    [
        "https://github.com/someone/core",
        "https://github.com/someone/core/",
        "github.com/someone/core.git",
        "  someone/core  ",
    ],
)
def test_a_pasted_github_url_is_accepted_as_owner_name(pasted):
    assert pinner.normalize_repo(pasted) == FORK


def test_an_empty_repository_means_core():
    assert pinner.normalize_repo("") == CORE_REPO


async def test_a_malformed_repository_makes_no_request(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock
):
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/compare", "domain": FAKE_DOMAIN, "git_ref": "dev", "git_repo": "nope"}
    )
    msg = await client.receive_json()

    assert not msg["success"] and msg["error"]["code"] == "pin_error"
    assert aioclient_mock.call_count == 0


async def test_an_unknown_ref_in_a_fork_names_the_fork(
    hass: HomeAssistant, setup, aioclient_mock
):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    aioclient_mock.get(f"{_gh_api(FORK)}/commits/nope", status=404, json={"message": "Not Found"})

    with pytest.raises(pinner.PinError, match="someone/core"):
        await pinner.async_resolve_git_source(async_get_clientsession(hass), "nope", FORK)


async def test_a_repository_that_is_not_a_core_fork_says_so(
    hass: HomeAssistant, setup, aioclient_mock
):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    aioclient_mock.get(
        f"{_gh_api(FORK)}/commits/main",
        json={"sha": FAKE_SHA, "commit": {"committer": {"date": ""}}},
    )
    aioclient_mock.get(f"{_gh_raw(FORK)}/{FAKE_SHA}/homeassistant/const.py", status=404)

    with pytest.raises(pinner.PinError, match="fork of home-assistant/core"):
        await pinner.async_resolve_git_source(async_get_clientsession(hass), "main", FORK)


async def test_a_fork_without_that_integration_names_the_fork(
    hass: HomeAssistant, setup, aioclient_mock
):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    aioclient_mock.get(
        f"{_gh_api(FORK)}/git/trees/{FAKE_SHA}:homeassistant/components/nope",
        status=404,
        json={"message": "Not Found"},
    )
    source = pinner.GitSource(ref="dev", sha=FAKE_SHA, core_version="2026.10.0.dev0", repo=FORK)

    with pytest.raises(pinner.PinError, match="someone/core at dev has no core integration 'nope'"):
        await pinner.async_git_component(async_get_clientsession(hass), source, "nope")


async def test_pinning_from_a_fork_records_the_repository(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, tmp_path
):
    _mock_github_ref(aioclient_mock, repo=FORK)
    _mock_github_tree(
        aioclient_mock,
        FAKE_DOMAIN,
        {"__init__.py": b"forked fix", "manifest.json": b'{"domain": "sun", "requirements": []}'},
        repo=FORK,
    )
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "git_ref": "dev", "git_repo": FORK}
    )
    assert (await client.receive_json())["success"]

    override = tmp_path / "custom_components" / FAKE_DOMAIN
    assert alias.strip((override / "__init__.py").read_bytes()) == b"forked fix"
    assert json.loads((override / MARKER_FILE).read_text())["git_repo"] == FORK

    await client.send_json_auto_id({"type": f"{DOMAIN}/list"})
    (pin,) = (await client.receive_json())["result"]["pins"]
    assert pin["source"] == "git"
    assert pin["git_repo"] == FORK
    assert pin["git_sha"] == FAKE_SHA


async def test_pinning_from_core_records_core_as_the_repository(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock
):
    _mock_github_ref(aioclient_mock)
    _mock_github_tree(
        aioclient_mock, FAKE_DOMAIN, {"__init__.py": b"x", "manifest.json": b'{"domain": "sun"}'}
    )
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "git_ref": "dev"}
    )
    assert (await client.receive_json())["result"]["git_repo"] == CORE_REPO


async def test_compare_against_a_fork_labels_it_with_the_repository(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock
):
    _mock_github_ref(aioclient_mock, repo=FORK)
    _mock_github_tree(
        aioclient_mock,
        FAKE_DOMAIN,
        {"__init__.py": b"new", "manifest.json": b'{"domain": "sun"}'},
        repo=FORK,
    )
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/compare", "domain": FAKE_DOMAIN, "git_ref": "dev", "git_repo": FORK}
    )
    result = (await client.receive_json())["result"]

    assert result["version"] == "someone/core@dev @ 90258e6"
    assert result["identical"] is False


async def test_compare_baselines_a_fork_pin_against_the_fork(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock
):
    """Only the fork is mocked, so a baseline fetched from core would fail the request."""
    _mock_github_ref(aioclient_mock, repo=FORK)
    _mock_github_tree(
        aioclient_mock,
        FAKE_DOMAIN,
        {"__init__.py": b"old", "manifest.json": b'{"domain": "sun"}'},
        repo=FORK,
    )
    _mock_github_ref(aioclient_mock, ref="fix", sha=OTHER_SHA, repo=FORK)
    _mock_github_tree(
        aioclient_mock,
        FAKE_DOMAIN,
        {"__init__.py": b"new", "manifest.json": b'{"domain": "sun"}'},
        sha=OTHER_SHA,
        repo=FORK,
    )
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "git_ref": "dev", "git_repo": FORK}
    )
    assert (await client.receive_json())["success"]

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/compare", "domain": FAKE_DOMAIN, "git_ref": "fix", "git_repo": FORK}
    )
    result = (await client.receive_json())["result"]

    assert result["compared_with"] == "pinned 2026.10.0.dev0+90258e6"
    assert result["changed"] == ["__init__.py"]


async def test_repinning_a_fork_pin_by_its_commit_refetches_from_the_fork(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, tmp_path
):
    """The panel's Re-pin sends the recorded sha and repo, not the version string."""
    files = {"__init__.py": b"forked fix", "manifest.json": b'{"domain": "sun"}'}
    _mock_github_ref(aioclient_mock, repo=FORK)
    _mock_github_ref(aioclient_mock, ref=FAKE_SHA, repo=FORK)
    _mock_github_tree(aioclient_mock, FAKE_DOMAIN, files, repo=FORK)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "git_ref": "dev", "git_repo": FORK}
    )
    assert (await client.receive_json())["success"]
    override = tmp_path / "custom_components" / FAKE_DOMAIN
    import shutil

    shutil.rmtree(override)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "git_ref": FAKE_SHA, "git_repo": FORK}
    )
    repinned = (await client.receive_json())["result"]

    assert alias.strip((override / "__init__.py").read_bytes()) == b"forked fix"
    assert repinned["git_repo"] == FORK and repinned["git_sha"] == FAKE_SHA


def test_a_pin_stored_before_forks_loads_as_a_core_pin():
    from custom_components.integration_pins.store import Pin

    pin = Pin.from_dict(
        {
            "domain": "sun",
            "pinned_version": "2026.10.0.dev0+90258e6",
            "core_range": "",
            "source": "git",
            "git_ref": "dev",
            "git_sha": FAKE_SHA,
        }
    )

    assert pin.git_repo == CORE_REPO


# -- uploads ---------------------------------------------------------------------

UPLOAD_URL = "/api/integration_pins/upload"
UPLOADED = {
    "__init__.py": b"my local fix",
    "manifest.json": b'{"domain": "sun", "requirements": []}',
    "translations/en.json": b"{}",
}


def _upload_form(domain, files, label="sun"):
    form = aiohttp.FormData()
    form.add_field("domain", domain)
    form.add_field("label", label)
    for path, data in files.items():
        form.add_field(
            f"f:{path}",
            data,
            filename=path.rsplit("/", 1)[-1],
            content_type="application/octet-stream",
        )
    return form


async def _upload(hass_client, domain=FAKE_DOMAIN, files=UPLOADED, token=None):
    client = await (hass_client() if token is None else hass_client(token))
    return await client.post(UPLOAD_URL, data=_upload_form(domain, files))


async def _staged(hass_client, **kwargs) -> dict:
    resp = await _upload(hass_client, **kwargs)
    assert resp.status == 200, await resp.text()
    return await resp.json()


async def test_uploading_a_folder_stages_it_without_writing_anything(
    hass: HomeAssistant, setup, hass_client, tmp_path
):
    staged = await _staged(hass_client)

    assert staged["domain"] == FAKE_DOMAIN
    assert staged["files"] == 3
    assert staged["version"].startswith(f"{CORE_VERSION}+local.")
    assert len(staged["version"].rsplit(".", 1)[-1]) == 7
    assert not (tmp_path / "custom_components" / FAKE_DOMAIN).exists()


async def test_upload_version_follows_the_content(hass: HomeAssistant, setup, hass_client):
    first = await _staged(hass_client)
    same = await _staged(hass_client)
    other = await _staged(hass_client, files={**UPLOADED, "__init__.py": b"another fix"})

    assert first["version"] == same["version"] != other["version"]
    assert first["upload_id"] != same["upload_id"]


async def test_upload_is_hashed_off_the_event_loop(
    hass: HomeAssistant, setup, hass_client, monkeypatch
):
    """An upload can be 20 MB; hashing it for the version must not stall the loop."""
    from custom_components.integration_pins import uploads

    on_loop: list[bool] = []
    original = uploads.content_version

    def spy(*args):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return original(*args)

    monkeypatch.setattr(uploads, "content_version", spy)

    await _staged(hass_client)

    assert on_loop == [False]


async def test_upload_drops_bytecode(hass: HomeAssistant, setup, hass_client):
    staged = await _staged(
        hass_client,
        files={**UPLOADED, "__pycache__/x.cpython-313.pyc": b"junk", "stray.pyc": b"junk"},
    )

    assert staged["files"] == 3


async def test_upload_requires_an_admin(
    hass: HomeAssistant, setup, hass_client, hass_read_only_access_token
):
    resp = await _upload(hass_client, token=hass_read_only_access_token)

    assert resp.status == 401


@pytest.mark.parametrize(
    ("domain", "files", "message"),
    [
        ("hue", UPLOADED, "'sun' integration, not 'hue'"),
        (FAKE_DOMAIN, {"__init__.py": b"x"}, "no manifest.json"),
        (FAKE_DOMAIN, {**UPLOADED, "manifest.json": b"{not json"}, "not valid JSON"),
        (FAKE_DOMAIN, {**UPLOADED, "../evil.py": b"x"}, "outside"),
        (FAKE_DOMAIN, {**UPLOADED, "/etc/evil.py": b"x"}, "outside"),
        (FAKE_DOMAIN, {**UPLOADED, "a\\evil.py": b"x"}, "outside"),
        (FAKE_DOMAIN, {**UPLOADED, "x/": b"x"}, "not a file path"),
        (FAKE_DOMAIN, {**UPLOADED, "a//b.py": b"x"}, "not a file path"),
        (FAKE_DOMAIN, {**UPLOADED, "./b.py": b"x"}, "not a file path"),
        (FAKE_DOMAIN, {**UPLOADED, "a": b"x", "a/b.py": b"x"}, "both a file and a directory"),
        ("", UPLOADED, "No integration domain"),
    ],
    ids=[
        "domain_mismatch",
        "no_manifest",
        "bad_manifest",
        "dotdot",
        "absolute",
        "backslash",
        "trailing_slash",
        "empty_segment",
        "dot_segment",
        "file_and_dir",
        "no_domain",
    ],
)
async def test_upload_is_refused(
    hass: HomeAssistant, setup, hass_client, tmp_path, domain, files, message
):
    resp = await _upload(hass_client, domain=domain, files=files)

    assert resp.status == 400
    assert message in (await resp.json())["message"]
    assert not (tmp_path.parent / "evil.py").exists()


async def test_upload_of_too_many_files_points_at_the_checkout(
    hass: HomeAssistant, setup, hass_client, monkeypatch
):
    from custom_components.integration_pins import uploads

    monkeypatch.setattr(uploads, "MAX_FILES", 2)

    resp = await _upload(hass_client)

    assert resp.status == 400
    assert "not the whole checkout" in (await resp.json())["message"]


async def test_upload_over_the_size_limit_is_refused(
    hass: HomeAssistant, setup, hass_client, monkeypatch
):
    from custom_components.integration_pins import uploads

    monkeypatch.setattr(uploads, "MAX_BYTES", 16)

    resp = await _upload(hass_client)

    assert resp.status == 400
    assert "larger than" in (await resp.json())["message"]


def _an_upload():
    from custom_components.integration_pins import uploads

    return uploads.Upload(domain="sun", label="sun", files=dict(UPLOADED), version="1+local.x")


def test_staged_uploads_expire_and_the_oldest_is_evicted():
    from custom_components.integration_pins import uploads

    now = [0.0]
    staging = uploads.UploadStaging(clock=lambda: now[0])

    first = staging.add(_an_upload())
    now[0] = uploads.UPLOAD_TTL + 1
    with pytest.raises(pinner.PinError, match="That upload has expired; pick the folder again"):
        staging.get(first)

    ids = [staging.add(_an_upload()) for _ in range(uploads.MAX_UPLOADS + 1)]
    with pytest.raises(pinner.PinError, match="expired"):
        staging.get(ids[0])
    assert staging.get(ids[-1]).domain == "sun"


async def test_expired_uploads_are_freed_without_another_request(
    hass: HomeAssistant, setup, hass_client
):
    from datetime import timedelta

    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from homeassistant.util import dt as dt_util

    from custom_components.integration_pins import uploads

    staging = hass.data[DOMAIN].uploads
    now = [0.0]
    staging._clock = lambda: now[0]
    await _staged(hass_client)
    assert staging._uploads

    now[0] = uploads.UPLOAD_TTL + 1
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=uploads.UPLOAD_TTL + 1))
    await hass.async_block_till_done()

    assert not staging._uploads


async def test_compare_against_an_uploaded_folder(
    hass: HomeAssistant, setup, hass_client, hass_ws_client
):
    staged = await _staged(hass_client)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/compare", "domain": FAKE_DOMAIN, "upload_id": staged["upload_id"]}
    )
    result = (await client.receive_json())["result"]

    assert result["version"] == "upload sun"
    assert result["compared_with"] == f"core {CORE_VERSION}"
    assert "__init__.py" in result["changed"]
    assert result["identical"] is False


async def test_uploading_the_running_code_compares_identical(
    hass: HomeAssistant, setup, hass_client, hass_ws_client
):
    root = pinner.core_components_dir() / FAKE_DOMAIN
    files = {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }
    staged = await _staged(hass_client, files=files)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/compare", "domain": FAKE_DOMAIN, "upload_id": staged["upload_id"]}
    )

    assert (await client.receive_json())["result"]["identical"] is True


async def test_pinning_an_uploaded_folder(
    hass: HomeAssistant, setup, hass_client, hass_ws_client, tmp_path
):
    staged = await _staged(hass_client)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "upload_id": staged["upload_id"]}
    )
    pin = (await client.receive_json())["result"]

    override = tmp_path / "custom_components" / FAKE_DOMAIN
    assert alias.strip((override / "__init__.py").read_bytes()) == b"my local fix"
    assert (override / "translations" / "en.json").is_file()
    assert json.loads((override / "manifest.json").read_text())["version"] == staged["version"]
    assert json.loads((override / MARKER_FILE).read_text())["upload_label"] == "sun"
    assert pin["source"] == "upload"
    assert pin["upload_label"] == "sun"
    assert pin["pinned_version"] == staged["version"]

    await client.send_json_auto_id({"type": f"{DOMAIN}/list"})
    (listed,) = (await client.receive_json())["result"]["pins"]
    assert listed["status"] == "pending"

    # Pinning consumed the staged copy.
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "upload_id": staged["upload_id"]}
    )
    again = await client.receive_json()
    assert not again["success"] and "expired" in again["error"]["message"]


async def test_pinning_an_unknown_upload_is_refused(hass: HomeAssistant, setup, hass_ws_client):
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "upload_id": "gone"}
    )
    msg = await client.receive_json()

    assert not msg["success"] and msg["error"]["code"] == "pin_error"
    assert "pick the folder again" in msg["error"]["message"]


async def test_an_upload_cannot_be_pinned_under_another_domain(
    hass: HomeAssistant, setup, hass_client, hass_ws_client
):
    staged = await _staged(hass_client)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": "hue", "upload_id": staged["upload_id"]}
    )
    msg = await client.receive_json()

    assert not msg["success"]
    assert "'sun'" in msg["error"]["message"] and "'hue'" in msg["error"]["message"]


async def test_pin_takes_exactly_one_source(
    hass: HomeAssistant, setup, hass_client, hass_ws_client
):
    staged = await _staged(hass_client)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/pin",
            "domain": FAKE_DOMAIN,
            "version": "2025.1.0",
            "upload_id": staged["upload_id"],
        }
    )
    msg = await client.receive_json()

    assert not msg["success"] and msg["error"]["code"] == "pin_error"


async def test_unpinning_an_upload_retires_it(
    hass: HomeAssistant, setup, hass_client, hass_ws_client, tmp_path
):
    """An uploaded folder exists nowhere Home Assistant can fetch it from again."""
    staged = await _staged(hass_client)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "upload_id": staged["upload_id"]}
    )
    assert (await client.receive_json())["success"]

    await client.send_json_auto_id({"type": f"{DOMAIN}/unpin", "domain": FAKE_DOMAIN})
    assert (await client.receive_json())["success"]

    retired = list((tmp_path / RETIRED_DIRNAME).iterdir())
    assert len(retired) == 1 and retired[0].name.startswith(f"{FAKE_DOMAIN}-")
    assert alias.strip((retired[0] / "__init__.py").read_bytes()) == b"my local fix"


@pytest.mark.parametrize("issue", ["pin_missing", "pin_foreign"])
def test_repair_advice_covers_uploaded_pins(issue):
    """An uploaded pin has no Re-pin: its only way back is picking the folder again."""
    root = Path(__file__).parent.parent / "custom_components" / DOMAIN
    for name in ("strings.json", "translations/en.json"):
        text = json.loads((root / name).read_text())["issues"][issue]["description"]
        assert "pick its folder again" in text, name


# ---------------------------------------------------------------------------
# Pinning over a directory this integration did not write
# ---------------------------------------------------------------------------


async def _foreign_sun(hass: HomeAssistant, tmp_path: Path) -> Path:
    """custom_components/sun put there by something else, as the loader found it."""
    path = _write_custom_integration(tmp_path, FAKE_DOMAIN, name="Sun Plus", version="3.1.0")
    (path / "__init__.py").write_text("someone else's code\n")
    await _load_custom(hass, path)
    return path


def _files(path: Path) -> dict:
    return {p.relative_to(path): p.read_bytes() for p in path.rglob("*") if p.is_file()}


async def test_pinning_over_a_foreign_directory_is_refused_until_confirmed(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, fake_wheel, tmp_path
):
    foreign = await _foreign_sun(hass, tmp_path)
    before = _files(foreign)
    _mock_pypi(aioclient_mock, fake_wheel)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "version": FAKE_VERSION}
    )
    msg = await client.receive_json()

    assert msg["error"]["code"] == "foreign_directory_not_confirmed"
    assert msg["error"]["translation_placeholders"] == {
        "domain": FAKE_DOMAIN,
        "name": "Sun Plus",
        "version": "3.1.0",
        "retired_dir": RETIRED_DIRNAME,
    }
    assert _files(foreign) == before
    assert not (tmp_path / RETIRED_DIRNAME).exists()
    assert not any(url.path.endswith(".whl") for _, url, _, _ in aioclient_mock.mock_calls)


async def test_a_confirmed_pin_moves_the_foreign_directory_aside(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, fake_wheel, tmp_path
):
    foreign = await _foreign_sun(hass, tmp_path)
    before = _files(foreign)
    _mock_pypi(aioclient_mock, fake_wheel)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/pin",
            "domain": FAKE_DOMAIN,
            "version": FAKE_VERSION,
            "confirm_replace_foreign": True,
        }
    )
    assert (await client.receive_json())["success"]

    (retired,) = (tmp_path / RETIRED_DIRNAME).iterdir()
    assert retired.name.startswith(f"{FAKE_DOMAIN}-replaced-")
    assert _files(retired) == before  # moved whole, nothing deleted
    marker = json.loads((foreign / MARKER_FILE).read_text())
    assert marker["pinned_version"] == FAKE_VERSION


async def test_repinning_over_our_own_override_needs_no_confirmation(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, tmp_path
):
    for version in (FAKE_VERSION, "2026.1.0"):
        _mock_pypi(aioclient_mock, build_fake_wheel(tmp_path, version=version), version=version)
    client = await hass_ws_client(hass)
    for version in (FAKE_VERSION, "2026.1.0"):
        await client.send_json_auto_id(
            {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "version": version}
        )
        assert (await client.receive_json())["success"], version


async def test_restoring_a_replaced_pin_asks_first(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, fake_wheel, tmp_path
):
    """Re-pin over a directory that lost our marker is pinning over someone else's code."""
    _mock_pypi(aioclient_mock, fake_wheel)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "version": FAKE_VERSION}
    )
    assert (await client.receive_json())["success"]
    (tmp_path / "custom_components" / FAKE_DOMAIN / MARKER_FILE).unlink()

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "version": FAKE_VERSION}
    )
    msg = await client.receive_json()

    assert msg["error"]["code"] == "foreign_directory_not_confirmed"
    # Not in the loader: the version comes from the manifest the panel already reads.
    assert msg["error"]["translation_placeholders"]["version"] == FAKE_VERSION


async def test_git_and_upload_pins_ask_before_replacing_too(
    hass: HomeAssistant, setup, hass_ws_client, hass_client, aioclient_mock, tmp_path
):
    foreign = await _foreign_sun(hass, tmp_path)
    before = _files(foreign)
    _mock_github_ref(aioclient_mock)
    _mock_github_tree(aioclient_mock, FAKE_DOMAIN, {"__init__.py": b"x", "manifest.json": b'{"domain": "sun"}'})
    staged = await _staged(hass_client)
    client = await hass_ws_client(hass)

    for source in ({"git_ref": "dev"}, {"upload_id": staged["upload_id"]}):
        await client.send_json_auto_id({"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, **source})
        msg = await client.receive_json()
        assert msg["error"]["code"] == "foreign_directory_not_confirmed", source

    assert _files(foreign) == before


async def test_compare_says_up_front_when_pinning_would_replace_a_foreign_directory(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, fake_wheel, tmp_path
):
    _mock_pypi(aioclient_mock, fake_wheel)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/compare", "domain": FAKE_DOMAIN, "version": FAKE_VERSION}
    )
    empty = (await client.receive_json())["result"]
    await _foreign_sun(hass, tmp_path)
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/compare", "domain": FAKE_DOMAIN, "version": FAKE_VERSION}
    )
    occupied = (await client.receive_json())["result"]

    assert empty["replaces_foreign"] is False and empty["foreign"] is None
    assert occupied["replaces_foreign"] is True
    assert occupied["foreign"] == {
        "domain": FAKE_DOMAIN,
        "name": "Sun Plus",
        "version": "3.1.0",
        "retired_dir": RETIRED_DIRNAME,
    }


# --- importers get the pinned code ---------------------------------------------------

_PROBE = "pins_alias_probe"  # not a core domain, so nothing else ever imports either name


@pytest.mark.parametrize(
    ("source", "before"),
    [
        ('"""Doc."""\n\nfrom __future__ import annotations\n\nimport os\n', 3),
        ('"""A docstring\n\nover lines."""\nfrom __future__ import annotations\n', 4),
        ('"""Doc."""\n', 1),
        ("import os\n", 0),
        ("", 0),
        ("x = (\n", 0),  # does not parse; it will not import either way
        ('"""Doc."""\r\nfrom __future__ import annotations\r\nimport os\r\n', 2),
    ],
)
def test_the_alias_goes_after_the_docstring_and_future_imports(source, before):
    injected = alias.inject(source)

    assert injected.splitlines(keepends=True)[before] == alias.INIT_LINE
    assert alias.strip(injected.encode()) == source.encode()
    if source != "x = (\n":
        compile(injected, "__init__.py", "exec")  # a __future__ import still comes first


@pytest.fixture
def probe_root(tmp_path: Path):
    """A pin of _PROBE written the way every pin is, importable as custom_components."""
    root = tmp_path / "probe"
    files = {
        "__init__.py": b'"""Probe."""\n\nfrom __future__ import annotations\n\nfrom .const import VALUE\n',
        "const.py": b'VALUE = "pinned"\n',
        "entity.py": b"from .const import VALUE\n\n\nclass Thing:\n    value = VALUE\n",
        "manifest.json": json.dumps({"domain": _PROBE, "name": "Probe"}).encode(),
    }
    pinner.install_component(files, _PROBE, "1.0.0", str(root), CORE_VERSION)
    sys.path.insert(0, str(root))
    yield root
    sys.path.remove(str(root))
    for name in [n for n in sys.modules if _PROBE in n]:
        del sys.modules[name]
    sys.meta_path[:] = [f for f in sys.meta_path if getattr(f, "pinned", None) != f"custom_components.{_PROBE}"]
    import homeassistant.components as components

    components.__dict__.pop(_PROBE, None)


def test_importing_the_bundled_name_gets_the_pinned_modules(probe_root):
    pinned = importlib.import_module(f"custom_components.{_PROBE}")

    # entity is not imported by the pin itself, so this loads it fresh through the alias.
    entity = importlib.import_module(f"homeassistant.components.{_PROBE}.entity")
    import homeassistant.components

    assert importlib.import_module(f"homeassistant.components.{_PROBE}") is pinned
    assert getattr(homeassistant.components, _PROBE).const.VALUE == "pinned"
    assert entity is sys.modules[f"custom_components.{_PROBE}.entity"]
    assert entity.__name__ == entity.__spec__.name == f"custom_components.{_PROBE}.entity"
    assert entity.Thing.value == "pinned"
    assert alias.importers(_PROBE) == alias.IMPORTERS_PINNED


def test_a_module_the_pin_lacks_fails_to_import_rather_than_coming_from_core(probe_root):
    importlib.import_module(f"custom_components.{_PROBE}")

    with pytest.raises(ModuleNotFoundError, match=f"custom_components.{_PROBE}.missing"):
        importlib.import_module(f"homeassistant.components.{_PROBE}.missing")


def test_a_bundled_package_imported_first_is_left_alone(probe_root, caplog):
    bundled = types.ModuleType(f"homeassistant.components.{_PROBE}")
    sys.modules[bundled.__name__] = bundled
    finders = list(sys.meta_path)

    importlib.import_module(f"custom_components.{_PROBE}")

    assert sys.modules[bundled.__name__] is bundled
    assert sys.modules[f"custom_components.{_PROBE}._integration_pins_alias"].ALIASED is False
    assert sys.meta_path == finders
    assert "imported before this pin loaded" in caplog.text
    assert alias.importers(_PROBE) == alias.IMPORTERS_BUNDLED


async def test_a_release_pin_carries_the_alias_and_still_compares_identical(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, fake_wheel, tmp_path
):
    _mock_pypi(aioclient_mock, fake_wheel)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "version": FAKE_VERSION}
    )
    assert (await client.receive_json())["success"]
    override = tmp_path / "custom_components" / FAKE_DOMAIN

    assert (override / alias.SHIM_FILE).read_text() == alias.SHIM_SOURCE
    assert alias.INIT_LINE in (override / "__init__.py").read_text()
    original = f'"""fake {FAKE_DOMAIN} from {FAKE_VERSION}"""\n'.encode()
    digest = pinner.local_component_digest(override, {MARKER_FILE})
    assert alias.SHIM_FILE not in digest
    assert digest["__init__.py"] == pinner.digest_files({"x": original})["x"]


async def test_adopting_leaves_the_code_as_it_was_placed(
    hass: HomeAssistant, setup, hass_ws_client, tmp_path
):
    manual = tmp_path / "custom_components" / "hue"
    manual.mkdir(parents=True)
    (manual / "manifest.json").write_text(json.dumps({"domain": "hue", "version": "2025.11.0"}))
    (manual / "__init__.py").write_text("hand placed\n")
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/adopt", "domain": "hue", "pinned_version": "2025.11.0", "core_range": ""}
    )
    assert (await client.receive_json())["success"]

    assert (manual / "__init__.py").read_text() == "hand placed\n"
    assert not (manual / alias.SHIM_FILE).exists()


async def _running_pin_with_importers(hass, client, aioclient_mock, fake_wheel, tmp_path, monkeypatch, bundled):
    """Pin FAKE_DOMAIN and make it the running code, with `bundled` as what importers got."""
    _mock_pypi(aioclient_mock, fake_wheel)
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "version": FAKE_VERSION}
    )
    assert (await client.receive_json())["success"]
    override = tmp_path / "custom_components" / FAKE_DOMAIN
    _mark_loaded(hass, FAKE_DOMAIN, override)
    pinned = types.ModuleType(f"custom_components.{FAKE_DOMAIN}")
    monkeypatch.setitem(sys.modules, pinned.__name__, pinned)
    monkeypatch.setitem(
        sys.modules,
        f"homeassistant.components.{FAKE_DOMAIN}",
        pinned if bundled is None else bundled,
    )
    return override


async def _recheck(client) -> dict:
    await client.send_json_auto_id({"type": f"{DOMAIN}/update", "domain": FAKE_DOMAIN, "reason": "x"})
    assert (await client.receive_json())["success"]
    await client.send_json_auto_id({"type": f"{DOMAIN}/list"})
    (pin,) = (await client.receive_json())["result"]["pins"]
    return pin


async def test_a_pin_whose_importers_got_the_bundled_code_raises_a_repair(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, fake_wheel, tmp_path, monkeypatch
):
    client = await hass_ws_client(hass)
    bundled = types.ModuleType(f"homeassistant.components.{FAKE_DOMAIN}")
    await _running_pin_with_importers(hass, client, aioclient_mock, fake_wheel, tmp_path, monkeypatch, bundled)

    pin = await _recheck(client)

    assert pin["status"] == "active" and pin["importers"] == "bundled"
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"pin_not_aliased_{FAKE_DOMAIN}")

    monkeypatch.setitem(
        sys.modules, bundled.__name__, sys.modules[f"custom_components.{FAKE_DOMAIN}"]
    )
    pin = await _recheck(client)

    assert pin["importers"] == "pinned"
    assert not ir.async_get(hass).async_get_issue(DOMAIN, f"pin_not_aliased_{FAKE_DOMAIN}")


async def test_a_pin_written_before_the_alias_only_says_so_in_the_panel(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, fake_wheel, tmp_path, monkeypatch
):
    client = await hass_ws_client(hass)
    bundled = types.ModuleType(f"homeassistant.components.{FAKE_DOMAIN}")
    override = await _running_pin_with_importers(
        hass, client, aioclient_mock, fake_wheel, tmp_path, monkeypatch, bundled
    )
    (override / alias.SHIM_FILE).unlink()

    pin = await _recheck(client)

    assert pin["importers"] == "bundled" and pin["override"]["alias"] is False
    assert not ir.async_get(hass).async_get_issue(DOMAIN, f"pin_not_aliased_{FAKE_DOMAIN}")


async def test_a_pending_pin_says_nothing_about_importers(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, fake_wheel
):
    _mock_pypi(aioclient_mock, fake_wheel)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "version": FAKE_VERSION}
    )
    assert (await client.receive_json())["success"]

    pin = await _recheck(client)

    assert pin["status"] == "pending" and pin["importers"] is None


@pytest.mark.parametrize(("domain", "preimported"), [("http", True), ("recorder", True), ("sun", False)])
async def test_domain_info_says_when_bootstrap_imports_the_domain_first(
    hass: HomeAssistant, setup, hass_ws_client, domain, preimported
):
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": f"{DOMAIN}/domain_info", "domain": domain})

    assert (await client.receive_json())["result"]["preimported"] is preimported
