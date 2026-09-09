# Integration Pins for Home Assistant

Run a specific release's copy of a core integration without downgrading Home Assistant.

When a core release ships a broken integration, you normally have two choices: live with it, or roll back the whole install. This custom integration adds a third one. It adds an **Integration Pins** sidebar panel where you can pick a core integration and the release you want its code from; it pulls that integration out of the release's wheel on PyPI and drops it into `custom_components/`, which Home Assistant loads in preference to the bundled copy. Everything else keeps running the current core.

Nothing is ever changed automatically on upgrade. Each pin carries a "valid for core" version range; when the running core leaves that range you get a Repair warning and the pinned code keeps running until you decide what to do.

## Install (Docker)

1. Copy `custom_components/integration_pins/` into your config directory, next to `configuration.yaml`, so you end up with `<config>/custom_components/integration_pins/manifest.json`.
2. Restart Home Assistant.
3. Settings → Devices & services → Add integration → **Integration Pins**. It has no options; adding it just registers the panel.
4. **Integration Pins** appears in the sidebar (admin users only).

No Docker or container changes are needed. The integration writes only inside your bind-mounted config directory (`custom_components/`, `integration_pins_retired/`, and `.storage/integration_pins`), so an Ansible-managed container is fine as-is. The container needs outbound HTTPS to `pypi.org` and `files.pythonhosted.org` to fetch wheels; that's the same access it already uses to install integration requirements.

## Using it

**Pin** — enter a core integration's domain (autocompletes from the running core), pick the release to take the code from, optionally set a core range and a reason, click Pin. The wheel for that release is downloaded (roughly 30–50 MB, verified against PyPI's sha256), the integration's directory is extracted into `custom_components/<domain>/`, and a `version` key is added to its manifest as custom integrations require. Restart to load it.

**Valid for core** is a PEP 440 specifier set evaluated against the running core version. `==2026.9.1` is the default (only the core you're on now), `~=2026.9.0` is any 2026.9.x patch release, `>=2026.9.0,<2026.11.0` is an explicit window, and empty means any. When the running core is outside the range the pin shows **Out of range** in the panel and a warning appears under Settings → Repairs. Widen the range if you've checked the pinned code still works, or unpin.

**Edit** changes the range or reason without touching files. **Unpin** moves `custom_components/<domain>/` to `integration_pins_retired/<domain>-<version>-<timestamp>/` and forgets the pin; restart to go back to the bundled integration. **Re-pin** appears when the override directory is missing or was replaced by something else, and downloads it again.

The panel also lists any other directory in `custom_components/` that shadows a core domain but isn't managed here (a manual copy, say). **Adopt** records it as a pin so it gets the same range checking, without changing its files.

Each pin shows how the pinned code's Python requirements differ from the bundled version's. Home Assistant installs whatever the loaded integration's manifest asks for at startup, so if the bug you're dodging lives in the integration's PyPI library the pin handles that too. If another integration needs the newer version of the same library you can end up with the two fighting over it across restarts; the panel points this out so you know to check.

## What can go wrong

The pinned code runs against a newer core than it was written for. Across one or two monthly releases this is almost always fine because core deprecates helpers with a long warning period, but it isn't guaranteed. After pinning, watch the log for deprecation warnings from `custom_components.<domain>` and skim the target release's breaking-changes notes for anything mentioning that integration. Pins on infrastructure-style integrations that other integrations import from (`bluetooth`, `mqtt`, `zha`, `recorder`, `http`, etc.) are much riskier than pins on ordinary device integrations; the tool doesn't stop you, but it's the wrong hammer for those.

Home Assistant logs a "custom integration ... has not been tested" warning for every override at startup. That's expected.

## Layout

```
custom_components/integration_pins/
  __init__.py       entry setup, static path + sidebar panel registration
  manager.py        orchestrates pins, evaluates ranges, syncs Repair issues
  pinner.py         PyPI lookup, wheel download/verify, extract/retire directories
  store.py          .storage/integration_pins persistence
  websocket.py      integration_pins/* websocket commands used by the panel
  config_flow.py    single-instance config entry
  panel/            the sidebar panel (plain web component, no build step)
tests/              pytest against a real Home Assistant core
docs/               screenshots from the end-to-end run
```

## Development

```
uv venv --python 3.13 .venv && . .venv/bin/activate
uv pip install homeassistant pytest-homeassistant-custom-component "home-assistant-frontend==<version from homeassistant/components/frontend/manifest.json>"
pytest
```

The tests exercise the full pin → out-of-range → unpin flow through the websocket API with a fake wheel served from an aiohttp mock; the panel was additionally checked in a browser against a live instance pinning a real integration from a real PyPI wheel.
