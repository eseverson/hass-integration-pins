"""End-to-end tests against a real Home Assistant core."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import __version__ as CORE_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.integration_pins import pinner
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


def _mock_pypi(aioclient_mock, fake_wheel: Path, version: str = FAKE_VERSION):
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
    assert (await client.receive_json())["success"]
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
    aioclient_mock.get(
        f"https://pypi.org/pypi/homeassistant/{FAKE_VERSION}/json",
        json={"urls": [{"packagetype": "bdist_wheel", "url": "https://files.example/ha.whl", "digests": {"sha256": "0" * 64}, "size": 1}]},
    )
    aioclient_mock.get("https://files.example/ha.whl", content=fake_wheel.read_bytes())
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "version": FAKE_VERSION})
    msg = await client.receive_json()
    assert not msg["success"] and "sha256" in msg["error"]["message"]
    assert not (tmp_path / "custom_components" / FAKE_DOMAIN).exists()


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
    (path / "manifest.json").write_text(json.dumps({"domain": domain, **manifest}))
    return path


async def test_snapshot_lists_third_party_custom_integrations(
    hass: HomeAssistant, setup, hass_ws_client, tmp_path
):
    _write_custom_integration(tmp_path, "my_hack", version="0.1.0")
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": f"{DOMAIN}/list"})
    snap = (await client.receive_json())["result"]

    assert snap["custom_integrations"] == [
        {"domain": "my_hack", "version": "0.1.0", "source": "manual", "source_detail": ""}
    ]


async def test_custom_integration_source_comes_from_hacs_storage(
    hass: HomeAssistant, setup, hass_ws_client, tmp_path
):
    _write_custom_integration(tmp_path, "alarmo", version="1.10.5")
    storage = tmp_path / ".storage"
    storage.mkdir(exist_ok=True)
    (storage / "hacs.data").write_text(
        json.dumps(
            {
                "data": {
                    "repositories": {
                        "1": {
                            "full_name": "nielsfaber/alarmo",
                            "domain": "alarmo",
                            "installed": True,
                        }
                    }
                }
            }
        )
    )
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": f"{DOMAIN}/list"})
    (entry,) = (await client.receive_json())["result"]["custom_integrations"]

    assert entry["source"] == "hacs"
    assert entry["source_detail"] == "nielsfaber/alarmo"


async def test_custom_integration_listing_excludes_pins_and_core_shadows(
    hass: HomeAssistant, setup, hass_ws_client, aioclient_mock, fake_wheel, tmp_path
):
    _mock_pypi(aioclient_mock, fake_wheel)
    _write_custom_integration(tmp_path, "hue", version="2025.11.0")  # shadows a core domain
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/pin", "domain": FAKE_DOMAIN, "version": FAKE_VERSION}
    )
    assert (await client.receive_json())["success"]

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
    assert (await client.receive_json())["success"]

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
    assert (await client.receive_json())["success"]

    retired = list((tmp_path / RETIRED_DIRNAME).iterdir())
    assert len(retired) == 1 and retired[0].name.startswith("hue-2025.11.0-")


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
    assert (await client.receive_json())["success"]

    assert list(root.iterdir()) == []


def test_parse_central_directory_matches_the_zip_it_came_from(tmp_path):
    import zipfile

    wheel = build_fake_wheel(tmp_path)
    with zipfile.ZipFile(wheel) as zf:
        expected = {i.filename: (i.CRC, i.file_size) for i in zf.infolist()}

    entries = pinner.parse_central_directory(wheel.read_bytes())

    assert entries == expected


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
