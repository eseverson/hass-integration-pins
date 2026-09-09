# Integration Pins for Home Assistant

Run a specific release's copy of a core integration without downgrading Home Assistant.

When a core release ships a broken integration you normally choose between living with it and rolling back the whole install. This adds a third option: an **Integration Pins** sidebar panel where you pick a core integration and the release you want its code from. It pulls that integration out of the release's wheel on PyPI into `custom_components/`, which Home Assistant loads in preference to the bundled copy. Everything else keeps running the current core.

![The Integration Pins panel](docs/panel.png)

Nothing is ever changed automatically on upgrade. Each pin carries a "valid for core" version range; when the running core leaves that range you get a Repair warning and the pinned code keeps running until you decide what to do.

## Install

1. Copy `custom_components/integration_pins/` into your config directory, next to `configuration.yaml`, so you end up with `<config>/custom_components/integration_pins/manifest.json`.
2. Restart Home Assistant.
3. Settings → Devices & services → Add integration → **Integration Pins**. It has no options; adding it registers the panel.
4. **Integration Pins** appears in the sidebar for admin users.

It writes only inside your config directory — `custom_components/`, `integration_pins_retired/` and `.storage/integration_pins` — and needs outbound HTTPS to `pypi.org` and `files.pythonhosted.org`, plus `api.github.com` and `raw.githubusercontent.com` if you pin from git.

## Using it

**Pin** — enter a core integration's domain, choose where the code comes from, optionally set a core range and a reason, click Pin. The domain box autocompletes from the integrations this instance is actually running; untick **only integrations in use** to search all ~1400 core domains. The release's wheel is downloaded and verified against PyPI's sha256, the integration's directory is extracted into `custom_components/<domain>/`, and a `version` key is added to its manifest as custom integrations require. Restart to load it.

**Taking the code from git** picks up a fix the moment it merges, before it ships. Enter a branch (`dev`), a tag or a commit; whatever you type is resolved to one immutable commit and that is what the pin records. It is far lighter than a wheel — roughly 100 KB against 30–50 MB — but sharper in two ways. The repository carries **English translations only**, because Home Assistant generates the rest at release time, so a git pin falls back to English for every other language. And `dev` is written against the **next** core release: pinning an older release is protected by core's deprecation policy, pinning ahead of your core is not, so the code can call helpers your core does not have yet. The panel says both when you switch the source to git.

**Compare with running code** says which files of the integration actually differ from what you are running — or that a release is byte-identical to it and pinning would change nothing. It reads only the file index rather than the code, so checking costs a fraction of a pin, and it works for a git ref too. If the integration is already pinned, the comparison is against the release or commit the pin came from rather than the files on disk.

The domain field links the integration's upstream history. **Changed since &lt;release&gt;** is usually the one you want: the commits that touched this integration between that release and now. The comparison links each changed file the same way, so "`light.py` differs" goes straight to the commits that changed it.

Some integrations are infrastructure. When other integrations are built on the one you entered, the form says how many depend on it and which of those are loaded here — `bluetooth` counts the dozens that reach it through `bluetooth_adapters`, not just the handful that name it directly.

**Valid for core** is a PEP 440 specifier set evaluated against the running core version. `==2026.9.1` is the default (only the core you are on now), `~=2026.9.0` is any 2026.9.x patch release, `>=2026.9.0,<2026.11.0` is an explicit window, and empty means any. When the running core falls outside the range the pin shows **Out of range** and a warning appears under Settings → Repairs. Widen the range if you have checked the pinned code still works, or unpin.

A new pin shows **Pending restart** until Home Assistant has loaded it. Custom integrations are scanned once at startup, so the files are in place but the bundled version is still running.

**Edit** changes the range or reason without touching files. **Unpin** removes `custom_components/<domain>/` and forgets the pin; restart to go back to the bundled integration. A pin from PyPI or git records exactly where its code came from, so those files are deleted outright and re-pinning fetches them again. A pin **adopted** from a directory you placed by hand exists nowhere else, so that one is moved to `integration_pins_retired/` and listed at the bottom of the panel with its size and a Delete button. **Re-pin** appears when the override directory has gone missing or been replaced.

The panel also lists directories in `custom_components/` that shadow a core domain but are not managed here, with where they came from. Several popular HACS integrations replace a core one this way — `plant` is the usual example — and those should be left to HACS, which installs and updates them; adopting one would put two things in charge of the same directory. **Adopt** records a genuinely hand-placed copy as a pin so it gets the same range checking, without changing its files.

A last section lists the third-party integrations that do not shadow a core domain, with their version and origin. Anything HACS manages is shown with its repository; anything else is unknown, since nothing on disk proves how it got there. None of it is pinnable — the section is there so you can see everything overriding or adding to core in one place.

Each pin shows how the pinned code's Python requirements differ from the bundled version's. Home Assistant installs whatever the loaded integration's manifest asks for at startup, so if the bug you are dodging lives in the integration's PyPI library the pin handles that too. If another integration needs a different version of the same library the two can fight over it across restarts, and the panel points that out so you know to check.

## What can go wrong

The pinned code runs against a newer core than it was written for. Across one or two monthly releases this is almost always fine, because core deprecates helpers with a long warning period, but it is not guaranteed. After pinning, watch the log for deprecation warnings from `custom_components.<domain>` and skim the target release's breaking-changes notes for anything mentioning that integration.

Pins on infrastructure integrations that others import from — `bluetooth`, `mqtt`, `zha`, `recorder`, `http` — are far riskier than pins on ordinary device integrations. The panel warns you and names the dependents loaded here, but it will not stop you; it is the wrong tool for those.

Home Assistant logs a "custom integration ... has not been tested" warning for every override at startup. That is expected.

## Development

```
uv venv --python 3.13 .venv && . .venv/bin/activate
uv pip install homeassistant pytest-homeassistant-custom-component "home-assistant-frontend==<version from homeassistant/components/frontend/manifest.json>"
pytest
```

```
custom_components/integration_pins/
  __init__.py       entry setup, panel registration
  manager.py        orchestrates pins, evaluates ranges, syncs Repair issues
  pinner.py         PyPI and GitHub lookup, download/verify, comparison, extract/retire
  store.py          .storage/integration_pins persistence
  websocket.py      integration_pins/* commands used by the panel
  config_flow.py    single-instance config entry
  panel/            the sidebar panel (plain web component, no build step)
```

The panel is served with `Cache-Control: no-cache` and its URL is keyed to a hash of the file, so editing `panel/integration-pins-panel.js` and reloading the browser is enough. Python only changes when Home Assistant restarts, which can leave the panel newer than its backend — so the backend advertises what it understands in `const.FEATURES` and the panel hides anything the running one does not list, rather than sending a request the older websocket schema would reject. Add a name to `FEATURES` whenever the panel starts depending on something the previous version could not do.

The tests drive the full pin → pending → out-of-range → unpin flow through the websocket API against a real Home Assistant core, with wheels and GitHub responses served from an aiohttp mock. The comparison was additionally checked against real wheels on PyPI: the same release reports byte-identical, an older one reports the files that actually changed.
