# Pinning from GitHub forks and uploaded folders

## Goal

Today a pin's code comes from a PyPI release or a commit in `home-assistant/core`. Add two more sources:

- **A GitHub fork of core** — a fix in someone's fork (or your own) that is not merged upstream.
- **A folder uploaded from the browser** — a local checkout on a machine Home Assistant cannot reach (e.g. the docker host, when HA runs in a container), but the browser can.

Out of scope: Forgejo/Gitea remotes, arbitrary git URLs, and standalone repos laid out as `custom_components/<domain>/`.

## Success criteria

- Pinning from `home-assistant/core` behaves exactly as before, and pins stored by the previous version load unchanged.
- A fork pin fetches `homeassistant/components/<domain>` from `owner/repo` at a ref, compares, pins, and links its commits in the fork.
- An uploaded integration folder can be compared and pinned through the same two-press flow, and unpinning it keeps a retired copy.
- An older backend under a newer panel hides both new options.

## 1. GitHub forks

**Layout.** The fork must use core's layout: the integration under `homeassistant/components/<domain>/`, the core version read from `homeassistant/const.py`. Anything else is refused with the existing "has no core integration" error naming the repo.

**Backend.**

- `const.py`: keep `GITHUB_REPO = "home-assistant/core"` as the default; replace `GITHUB_API_URL` / `GITHUB_RAW_URL` with helpers that build the URLs for a given repo.
- `pinner.GitSource` gains `repo: str` (default `GITHUB_REPO`). `async_resolve_git_source(session, ref, repo=GITHUB_REPO)` and `async_git_component` use `source.repo`. Error messages name the repo instead of hard-coding `home-assistant/core`.
- The repo is validated as `owner/name` (`^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$`) before any request; a 404 on the commits call reports "'<ref>' is not a branch, tag or commit in <repo>" (this also covers a repo that does not exist).
- `store.Pin` gains `git_repo: str = GITHUB_REPO`. `from_dict` already ignores unknown keys and defaults missing ones, so old pins load as core pins.
- `manager.async_compare`, `async_pin` and `_async_install_from_git` take `git_repo`; the compare baseline rebuilds `GitSource` with `pin.git_repo`.
- The marker's provenance records `git_repo` alongside `git_ref` / `git_sha`.
- `websocket`: `integration_pins/pin` and `integration_pins/compare` accept optional `git_repo` (default `GITHUB_REPO`).
- `FEATURES` gains `"git_repo"`.

**Panel.**

- With the git source selected, a **Repository** input sits beside the ref, prefilled `home-assistant/core`, shown only if the backend lists `git_repo`.
- The payload includes `git_repo`; the compare/pin cache key includes it.
- Pin rows show `owner/repo` in the git origin line when it is not core, and the commit link points at `https://github.com/<git_repo>/commit/<sha>`.
- The upstream history / "changed since" / per-file commit links use the selected repo.

## 2. Folder upload

**Picking.** A third source button, **a folder**, shown only if the backend lists `upload`. It opens an `<input type="file" webkitdirectory>` (Chrome, Firefox and Safari all support it). The user picks the integration's own directory, e.g. `…/core/homeassistant/components/zha` — not the whole checkout. Before uploading, the panel:

- drops anything under `__pycache__/` and `*.pyc`,
- strips the picked folder's own name from each `webkitRelativePath`,
- requires a top-level `manifest.json` whose `domain` equals the domain field; otherwise it shows a field error and does not upload.

**Transport.** `UploadView(HomeAssistantView)` at `POST /api/integration_pins/upload`, `requires_auth = True`, refused with 401 unless `request["hass_user"].is_admin`. The body is `multipart/form-data`: one `domain` field, then one file part per file, the part's filename being its path relative to the component directory. The panel calls it with `hass.fetchWithAuth`. HTTP rather than websocket because aiohttp caps websocket messages at 4 MB.

The view rejects (400, with a message the panel shows):

- a path that is absolute, contains `..`, or has a backslash,
- more than 2000 files or 20 MB in total,
- a missing or unparsable `manifest.json`, or one whose `domain` differs from the `domain` field.

On success it returns `{"upload_id", "domain", "files": <count>, "version"}`.

**Staging.** `manager` keeps uploads in memory: `upload_id` (a `secrets.token_urlsafe(16)`) → `Upload(domain, files: dict[str, bytes], digest, created, label)`, where `label` is the picked folder name the panel sends as a `label` field. Entries expire after 15 minutes and are pruned on each new upload; at most 4 are held, oldest evicted. An unknown or expired id raises `PinError("That upload has expired; pick the folder again")`.

**Versioning.** `version = f"{CORE_VERSION}+local.{sha256 over sorted (path, bytes)}[:7]"`. Nothing in the folder says which core it targets, so the form's default range stays `==<current core>`.

**Compare and pin.** `integration_pins/compare` and `integration_pins/pin` accept optional `upload_id`. Exactly one of `version`, `git_ref`, `upload_id` must be given (the existing "either/or" check becomes "exactly one of"). Compare uses the staged files as the candidate and runs the same migration probe; the label is `upload <label>`. Pin runs the migration guard, writes the files with `pinner.install_component`, and stores the pin with `source="upload"`, `upload_label=<label>`. The staged upload is dropped after a successful pin.

**Stored pin.** `const.SOURCE_UPLOAD = "upload"`; `store.Pin` gains `upload_label: str = ""`.

**Unpin / re-pin.** An upload cannot be fetched again, so `async_unpin` keeps a copy: `keep_a_copy` stays `pin.source not in (SOURCE_PYPI, SOURCE_GIT)`, which already covers it. The panel's unpin text says the copy goes to `integration_pins_retired/`, and **Re-pin** is not offered for an upload pin (it has no source to fetch from).

**Compare baseline.** When the running pin is an upload, the baseline is the on-disk copy minus the marker, via the existing `local_component_digest` fallback — no special case needed. The manifest differs from the uploaded one (pinning writes `version` into it), so `manifest.json` shows as changed; acceptable.

**Panel.**

- A translations caveat like git's: a checkout carries generated translations only if it was built, so expect English only.
- Pin rows for uploads show an `upload` tag and the label.
- `FEATURES` gains `"upload"`.

## Testing

- **Forks:** extend the aiohttp mock with a second repo path. Test compare and pin from a fork (URL paths hit, `git_repo` stored, marker provenance), a bad repo string refused before any request, a 404 fork message, and that a stored pin without `git_repo` loads as core.
- **Upload view:** multipart success; non-admin 401; domain mismatch, `..` path, missing manifest and oversize each 400.
- **Upload flow:** upload → compare (identical and changed cases) → pin → status pending → unpin → copy present in `integration_pins_retired/`; an expired or unknown `upload_id` gives the expiry error; passing two sources is refused.

## README

Document both sources under "Using it", including that the folder picker wants the component directory, not the checkout. No new outbound hosts: forks use the same `api.github.com` and `raw.githubusercontent.com`, and uploads need none.
