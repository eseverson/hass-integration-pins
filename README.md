# Integration Pins for Home Assistant

Run a specific release's copy of a core integration without downgrading Home Assistant.

When a core release ships a broken integration, you normally have two choices: live with it, or roll back the whole install. This custom integration adds a third one. It adds an **Integration Pins** sidebar panel where you can pick a core integration and the release you want its code from; it pulls that integration out of the release's wheel on PyPI and drops it into `custom_components/`, which Home Assistant loads in preference to the bundled copy. Everything else keeps running the current core.

Nothing is ever changed automatically on upgrade. Each pin carries a "valid for core" version range; when the running core leaves that range you get a Repair warning and the pinned code keeps running until you decide what to do.

## Install (Docker)

1. Copy `custom_components/integration_pins/` into your config directory, next to `configuration.yaml`, so you end up with `<config>/custom_components/integration_pins/manifest.json`.
2. Restart Home Assistant.
3. Settings → Devices & services → Add integration → **Integration Pins**. It has no options; adding it just registers the panel.
4. **Integration Pins** appears in the sidebar (admin users only).

No Docker or container changes are needed. The integration writes only inside your bind-mounted config directory (`custom_components/`, `integration_pins_retired/`, and `.storage/integration_pins`), so an Ansible-managed container is fine as-is. The container needs outbound HTTPS to `pypi.org` and `files.pythonhosted.org` to fetch wheels, and to `api.github.com` and `raw.githubusercontent.com` if you pin from git; the first two are the same access it already uses to install integration requirements.

## Using it

**Pin** — enter a core integration's domain, choose where the code comes from, optionally set a core range and a reason, click Pin. The domain box autocompletes from the integrations this instance is actually running rather than all ~1400 core domains; untick **only integrations in use** to search the full list. The wheel for that release is downloaded (roughly 30–50 MB, verified against PyPI's sha256), the integration's directory is extracted into `custom_components/<domain>/`, and a `version` key is added to its manifest as custom integrations require. Restart to load it.

**Taking the code from git** instead of a release picks up a fix the moment it merges, before it ships. Enter a branch (`dev`), a tag or a commit sha; whatever you enter is resolved to one immutable commit and that is what the pin records. This route is far lighter than a wheel — two GitHub API calls for the commit and the integration's file list, then the files themselves from `raw.githubusercontent.com`, which is roughly 100 KB against 30–50 MB. GitHub allows 60 unauthenticated API calls an hour, so about 30 git pins in that window.

Two things make a git pin sharper than a release pin. The repository ships **English translations only** — Home Assistant generates the rest at release time from Lokalise — so a git pin falls back to English for every other language. And `dev` is written against the **next** core release: pinning an older release is protected by core's deprecation policy, pinning ahead of your core is not, so the code can call helpers your core does not have yet. The panel says both when you switch the source to git.

**Compare with running code** answers whether the pin is worth making before you download anything. A wheel is a zip, and a zip's central directory records the CRC32 and size of every file in it, so one range request for that directory — under a tenth of the wheel — is enough to say exactly which files of the integration differ from the code you are running, or that the release is byte-identical to it and pinning would change nothing. It works for a git ref too. If the integration is already pinned, the comparison is made against the release or commit the pin came from rather than the files on disk, since pinning rewrites the manifest and adds a marker file.

Some integrations are infrastructure: when the domain you enter is one other integrations are built on, the form says how many core integrations depend on it and which of them are loaded here. That count is read from the manifests on disk and follows the dependency graph, so `bluetooth` reports the dozens of integrations that reach it through `bluetooth_adapters`, not just the handful that name it directly.

**Valid for core** is a PEP 440 specifier set evaluated against the running core version. `==2026.9.1` is the default (only the core you're on now), `~=2026.9.0` is any 2026.9.x patch release, `>=2026.9.0,<2026.11.0` is an explicit window, and empty means any. A freshly made pin shows **Pending restart** until Home Assistant has actually loaded it — custom integrations are scanned once at startup, so the files are in place but the bundled version is still running. When the running core is outside the range the pin shows **Out of range** in the panel and a warning appears under Settings → Repairs. Widen the range if you've checked the pinned code still works, or unpin.

**Edit** changes the range or reason without touching files. **Unpin** removes `custom_components/<domain>/` and forgets the pin; restart to go back to the bundled integration. A pin made from PyPI or git records exactly where the code came from, so its files are deleted outright and re-pinning fetches them again; a pin **adopted** from a directory you placed by hand exists nowhere else, so that one is moved to `integration_pins_retired/<domain>-<version>-<timestamp>/` instead. Anything in that folder is listed at the bottom of the panel with its size and a Delete button — nothing there is ever removed automatically. **Re-pin** appears when the override directory is missing or was replaced by something else, and downloads it again.

The panel also lists any other directory in `custom_components/` that shadows a core domain but isn't managed here, with where it came from. Several popular HACS integrations replace a core one this way — `plant` is the usual example — and those should be left to HACS, which installs and updates them; adopting one would put two things in charge of the same directory. **Adopt** records a genuinely hand-placed copy as a pin so it gets the same range checking, without changing its files.

A final section lists the third-party integrations in `custom_components/` that do not shadow a core domain, with their version and where they came from. HACS records what it installed in `.storage/hacs.data`, so anything it manages is shown with its repository; anything else is listed as unknown, since nothing on disk proves how it got there. Nothing there is pinnable — the section is there so you can see everything overriding or adding to core in one place.

Each pin shows how the pinned code's Python requirements differ from the bundled version's. Home Assistant installs whatever the loaded integration's manifest asks for at startup, so if the bug you're dodging lives in the integration's PyPI library the pin handles that too. If another integration needs the newer version of the same library you can end up with the two fighting over it across restarts; the panel points this out so you know to check.

## What can go wrong

The pinned code runs against a newer core than it was written for. Across one or two monthly releases this is almost always fine because core deprecates helpers with a long warning period, but it isn't guaranteed. After pinning, watch the log for deprecation warnings from `custom_components.<domain>` and skim the target release's breaking-changes notes for anything mentioning that integration. Pins on infrastructure-style integrations that other integrations import from (`bluetooth`, `mqtt`, `zha`, `recorder`, `http`, etc.) are much riskier than pins on ordinary device integrations; the panel warns you and names the dependents that are loaded here, but it doesn't stop you — it's the wrong hammer for those.

Home Assistant logs a "custom integration ... has not been tested" warning for every override at startup. That's expected.

## Layout

```
custom_components/integration_pins/
  __init__.py       entry setup, static path + sidebar panel registration
  manager.py        orchestrates pins, evaluates ranges, syncs Repair issues
  pinner.py         PyPI and GitHub lookup, download/verify, comparison, extract/retire directories
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

The panel is served with `Cache-Control: no-cache` and its URL is keyed to a hash of the file, so editing `panel/integration-pins-panel.js` and reloading the browser is enough — no restart, no hard reload. Python is not so lucky: `websocket.py` and the rest only change when Home Assistant restarts, so a panel reload can leave you newer on the frontend than on the backend. The backend advertises what it understands in `const.FEATURES`, and the panel hides anything the running one does not list and says a restart is needed, rather than sending a request the older websocket schema rejects. Add a name to `FEATURES` whenever the panel starts depending on something the previous version could not do.

The tests exercise the full pin → pending → out-of-range → unpin flow through the websocket API with a fake wheel served from an aiohttp mock; the panel was additionally checked in a browser against a live instance pinning a real integration from a real PyPI wheel. The release comparison was checked against real wheels on PyPI: the same release reports byte-identical, an older one reports the files that actually changed.
