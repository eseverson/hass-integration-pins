# Pinning from GitHub Forks and Uploaded Folders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a pin take its code from a GitHub fork of core, or from an integration folder uploaded through the browser.

**Architecture:** Forks generalize the existing GitHub fetch path by threading a `repo` through `GitSource`, the stored `Pin` and the websocket API. Uploads arrive at a new admin-only HTTP view as multipart, are staged in memory by a new `uploads.py` module, and are compared and pinned through the same websocket commands via a third source key, `upload_id`. The panel gains a repository field, a folder picker and the matching rows and links.

**Tech Stack:** Python 3.13, Home Assistant custom integration (aiohttp views, websocket_api, voluptuous), pytest with `pytest_homeassistant_custom_component`, plain web-component JS panel (no build step).

**Spec:** `docs/superpowers/specs/2026-09-22-arbitrary-sources-design.md`

## Global Constraints

- Default repository is `home-assistant/core`; pins stored before this change (no `git_repo` key) must load as core pins.
- Fork layout: `homeassistant/components/<domain>/`, core version read from `homeassistant/const.py`.
- Repository format `owner/name`, matching `^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$`, never containing `..`.
- Upload endpoint `POST /api/integration_pins/upload`, admin only, `multipart/form-data`.
- Upload limits: at most 2000 files, 20 MB total; absolute paths, `..` segments and backslashes rejected.
- Staged uploads live 15 minutes, at most 4 held, oldest evicted; unknown/expired id error text: `That upload has expired; pick the folder again`.
- Upload version: `f"{CORE_VERSION}+local.{sha256[:7]}"` over sorted `(path, bytes)`.
- Upload pins: `source="upload"`, `upload_label` stored; unpin retires them to `integration_pins_retired/`; no Re-pin.
- Compare and pin take exactly one of `version`, `git_ref`, `upload_id`.
- `FEATURES` gains `"git_repo"` and `"upload"`; the panel hides each option when the backend lacks it.
- American English; Conventional Commits (`feat(pins): …`), no Claude/Anthropic trailers; commit with explicit pathspecs.

## Review Focus

1. Picking the whole core checkout instead of the component folder — expect a clear "pick the integration's own directory" message, not a hang or a 20 MB upload (Task 2 test `test_upload_of_too_many_files_points_at_the_checkout`; panel check in Task 3).
2. Pasting a GitHub URL (`https://github.com/owner/core` or `…/core.git`) into the repository field — expect it to be accepted as `owner/core` (Task 1 test `test_a_pasted_github_url_is_accepted_as_owner_name`).
3. Re-pinning a git or fork pin whose override went missing — expect it to refetch the same commit from the same repository, not ask PyPI for `2026.10.0.dev0+90258e6` (Task 1 test `test_repinning_a_fork_pin_by_its_commit_refetches_from_the_fork`; panel fix in Task 3).
4. Waiting past 15 minutes (or restarting HA) between comparing and pinning an upload — expect "pick the folder again", and the panel to drop the stale upload (Task 2 tests `test_staged_uploads_expire_and_the_oldest_is_evicted`, `test_pinning_an_unknown_upload_is_refused`; panel in Task 3).
5. Uploading one integration's folder with another domain typed — expect an error naming both domains (Task 2 tests `test_upload_is_refused` param `domain_mismatch`, `test_an_upload_cannot_be_pinned_under_another_domain`).

---

## File structure

- Modify `custom_components/integration_pins/const.py` — repo default, regex, URL helpers, `SOURCE_UPLOAD`, `FEATURES`.
- Modify `custom_components/integration_pins/pinner.py` — `normalize_repo`, repo-aware `GitSource` / resolve / component fetch.
- Modify `custom_components/integration_pins/store.py` — `Pin.git_repo`, `Pin.upload_label`.
- Create `custom_components/integration_pins/uploads.py` — `Upload`, `UploadStaging`, validation, `content_version`, `read_upload`, `UploadView`.
- Modify `custom_components/integration_pins/manager.py` — `git_repo` and `upload_id` through compare/pin, `async_stage_upload`, staging instance.
- Modify `custom_components/integration_pins/websocket.py` — `git_repo`, `upload_id` schema keys.
- Modify `custom_components/integration_pins/__init__.py` — register `UploadView`.
- Modify `custom_components/integration_pins/panel/integration-pins-panel.js` — repo field, folder picker, rows, links, re-pin.
- Modify `tests/test_integration_pins.py` — fork and upload tests.
- Modify `README.md` — document both sources.

Run tests with: `. .venv/bin/activate && pytest -q` (baseline: 60 passed).

---

### Task 1: GitHub forks (backend)

**Files:**
- Modify: `custom_components/integration_pins/const.py`
- Modify: `custom_components/integration_pins/pinner.py:28-35` (imports), `:175-297` (git section)
- Modify: `custom_components/integration_pins/store.py:25-27`
- Modify: `custom_components/integration_pins/manager.py:150-170`, `:215-217`, `:306-409`
- Modify: `custom_components/integration_pins/websocket.py:127-150`, `:233-248`
- Test: `tests/test_integration_pins.py`

**Interfaces:**
- Produces: `const.GITHUB_REPO: str`, `const.github_api_url(repo: str) -> str`, `const.github_raw_url(repo: str) -> str`; `pinner.normalize_repo(repo: str) -> str`; `pinner.GitSource(ref, sha, core_version, date="", repo=GITHUB_REPO)`; `pinner.async_resolve_git_source(session, ref, repo=GITHUB_REPO) -> GitSource`; `Pin.git_repo: str = GITHUB_REPO`; `PinManager.async_compare(domain, version="", git_ref="", git_repo=GITHUB_REPO)`; `PinManager.async_pin(domain, version="", core_range="", reason="", git_ref="", acknowledge_migration=False, git_repo=GITHUB_REPO)`; websocket `pin`/`compare` accept `git_repo`.

- [x] **Step 1: Generalize the GitHub test helpers and write the failing fork tests**

In `tests/test_integration_pins.py`, replace the `GH_API` / `GH_RAW` constants and the two helpers so they take a repo (existing callers keep working through the default):

```python
CORE_REPO = "home-assistant/core"
FORK = "someone/core"


def _gh_api(repo=CORE_REPO):
    return f"https://api.github.com/repos/{repo}"


def _gh_raw(repo=CORE_REPO):
    return f"https://raw.githubusercontent.com/{repo}"


GH_API = _gh_api()
GH_RAW = _gh_raw()
```

```python
def _mock_github_ref(aioclient_mock, ref="dev", sha=FAKE_SHA, date="", repo=CORE_REPO):
    aioclient_mock.get(
        f"{_gh_api(repo)}/commits/{ref}",
        json={"sha": sha, "commit": {"committer": {"date": date}}},
    )
    aioclient_mock.get(f"{_gh_raw(repo)}/{sha}/homeassistant/const.py", text=FAKE_CONST_PY)
```

```python
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
```

Then append the fork tests at the end of the file:

```python
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
    assert (override / "__init__.py").read_bytes() == b"forked fix"
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

    assert (override / "__init__.py").read_bytes() == b"forked fix"
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
```

Also add to `test_snapshot_advertises_what_this_backend_can_do`, after the `"git"` assertion:

```python
    assert "git_repo" in snap["features"]
```

- [x] **Step 2: Run the new tests to verify they fail**

Run: `. .venv/bin/activate && pytest -q -k "fork or repository or pasted or malformed or before_forks or core_as_the or advertises"`
Expected: FAIL — `AttributeError: module ... has no attribute 'normalize_repo'`, `TypeError: ... unexpected keyword argument 'repo'`, `extra keys not allowed @ data['git_repo']`.

- [x] **Step 3: Add the repo constants and helpers to `const.py`**

Replace the three `GITHUB_*` lines with:

```python
# Git pins come from core or from a fork of it laid out the same way.
GITHUB_REPO = "home-assistant/core"
GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")


def github_api_url(repo: str) -> str:
    return f"https://api.github.com/repos/{repo}"


def github_raw_url(repo: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}"
```

Add `import re` under `from __future__ import annotations`, and append `"git_repo",` to `FEATURES`.

- [x] **Step 4: Make the git fetch path repo-aware in `pinner.py`**

In the `from .const import (...)` block, replace `GITHUB_API_URL, GITHUB_RAW_URL,` with `GITHUB_REPO, GITHUB_REPO_RE, github_api_url, github_raw_url,`.

Replace `GitSource` through the end of `async_git_component` with:

```python
@dataclass
class GitSource:
    """A resolved point in core, or in a fork of it."""

    ref: str  # what was asked for: a branch, a tag or a commit
    sha: str  # the commit it resolved to
    core_version: str  # the core version that commit builds
    date: str = ""  # YYYY-MM-DD the commit was made
    repo: str = GITHUB_REPO  # owner/name on GitHub

    @property
    def version(self) -> str:
        """Manifest version for code taken from this commit."""
        return f"{self.core_version}+{self.sha[:7]}"

    @property
    def where(self) -> str:
        """How error messages name the repository."""
        return "Home Assistant" if self.repo == GITHUB_REPO else self.repo


_GITHUB_PREFIXES = ("https://github.com/", "http://github.com/", "github.com/")


def normalize_repo(repo: str) -> str:
    """owner/name from what was typed or pasted; empty means core."""
    repo = repo.strip()
    for prefix in _GITHUB_PREFIXES:
        if repo.lower().startswith(prefix):
            repo = repo[len(prefix) :]
            break
    repo = repo.rstrip("/").removesuffix(".git") or GITHUB_REPO
    if not GITHUB_REPO_RE.match(repo) or ".." in repo:
        raise PinError(f"'{repo}' is not a GitHub repository; enter it as owner/name")
    return repo


async def _github_json(session: aiohttp.ClientSession, url: str, missing: str, **kwargs) -> Any:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30), **kwargs) as resp:
        if resp.status == 404:
            raise PinError(missing)
        if resp.status in (403, 429):
            raise PinError(
                "GitHub is rate limiting this instance (60 requests an hour without a "
                "token). Try again later."
            )
        if resp.status != 200:
            raise PinError(f"GitHub returned HTTP {resp.status} for {url}")
        return await resp.json()


def _parse_core_version(const_py: str) -> str:
    """Read __version__ out of homeassistant/const.py without executing it."""
    parts = []
    for name in _VERSION_PARTS:
        match = re.search(rf"^{name}_VERSION: Final = (.+)$", const_py, re.MULTILINE)
        if match is None:
            raise PinError("Could not read the core version at that commit")
        parts.append(match.group(1).strip().strip('"').strip("'"))
    return ".".join(parts)


async def async_resolve_git_source(
    session: aiohttp.ClientSession, ref: str, repo: str = GITHUB_REPO
) -> GitSource:
    """Pin a branch, tag or commit down to one immutable commit."""
    ref = ref.strip()
    repo = normalize_repo(repo)
    if not ref:
        raise PinError("Enter a branch, tag or commit to take the code from")

    data = await _github_json(
        session,
        f"{github_api_url(repo)}/commits/{ref}",
        f"'{ref}' is not a branch, tag or commit in {repo}",
    )
    sha = data.get("sha")
    if not sha:
        raise PinError(f"GitHub returned no commit for '{ref}'")

    async with session.get(
        f"{github_raw_url(repo)}/{sha}/homeassistant/const.py",
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        if resp.status == 404:
            raise PinError(
                f"{repo} has no homeassistant/const.py at {sha[:7]}; "
                f"is it a fork of {GITHUB_REPO}?"
            )
        if resp.status != 200:
            raise PinError(f"Could not read the core version at {sha[:7]}")
        const_py = await resp.text()

    committed = (data.get("commit", {}).get("committer", {}).get("date") or "")[:10]
    return GitSource(
        ref=ref, sha=sha, core_version=_parse_core_version(const_py), date=committed, repo=repo
    )


async def async_git_component(
    session: aiohttp.ClientSession, source: GitSource, domain: str
) -> dict[str, bytes]:
    """Every file of homeassistant/components/<domain>/ at a commit, keyed by path."""
    missing = f"{source.where} at {source.ref} has no core integration '{domain}'"
    data = await _github_json(
        session,
        f"{github_api_url(source.repo)}/git/trees/{source.sha}:homeassistant/components/{domain}",
        missing,
        params={"recursive": "1"},
    )
    if data.get("truncated"):
        raise PinError(f"'{domain}' is too large for GitHub to list in one request")

    paths = [
        entry["path"]
        for entry in data.get("tree", [])
        if entry.get("type") == "blob" and "__pycache__/" not in entry.get("path", "")
    ]
    if not paths:
        raise PinError(missing)

    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_BLOBS)
    raw = github_raw_url(source.repo)

    async def fetch(path: str) -> tuple[str, bytes]:
        url = f"{raw}/{source.sha}/homeassistant/components/{domain}/{path}"
        async with semaphore:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status != 200:
                    raise PinError(f"Could not fetch {path} at {source.sha[:7]}")
                return path, await resp.read()

    return dict(await asyncio.gather(*(fetch(path) for path in paths)))
```

Also update the section comment above `_VERSION_PARTS` if it names only `home-assistant/core`, so it says "core or a fork of it".

- [x] **Step 5: Store the repository on the pin (`store.py`)**

Add `from .const import GITHUB_REPO` to the imports and, after `git_sha`:

```python
    git_repo: str = GITHUB_REPO  # owner/name the commit came from
```

- [x] **Step 6: Thread `git_repo` through the manager**

In `manager.py`, add `GITHUB_REPO` to the `from .const import (...)` block.

`async_compare` signature and git branch:

```python
    async def async_compare(
        self, domain: str, version: str = "", git_ref: str = "", git_repo: str = GITHUB_REPO
    ) -> dict[str, Any]:
```

```python
        if git_ref:
            source = await pinner.async_resolve_git_source(session, git_ref, git_repo)
            files = await pinner.async_git_component(session, source, domain)
            candidate = pinner.digest_files(files)
            probe = {name: files[name] for name in MIGRATION_FILES if name in files}
            ref = source.ref if source.repo == GITHUB_REPO else f"{source.repo}@{source.ref}"
            label = f"{ref} @ {source.sha[:7]}"
            since = source.date
```

In `_async_baseline`:

```python
        if managed and pin.source == SOURCE_GIT and pin.git_sha:
            source = pinner.GitSource(
                pin.git_ref, pin.git_sha, pin.core_at_pin, repo=pin.git_repo
            )
```

`async_pin`: add `git_repo: str = GITHUB_REPO,` as the last parameter and pass it on:

```python
                pinned_version, provenance = await self._async_install_from_git(
                    domain, git_ref, session, acknowledge_migration, git_repo
                )
```

`_async_install_from_git`:

```python
    async def _async_install_from_git(
        self,
        domain: str,
        git_ref: str,
        session: Any,
        acknowledged: bool = False,
        git_repo: str = GITHUB_REPO,
    ) -> tuple[str, dict[str, str]]:
        source = await pinner.async_resolve_git_source(session, git_ref, git_repo)
        files = await pinner.async_git_component(session, source, domain)
        self._guard_migration(domain, files, acknowledged)
        _LOGGER.info(
            "Pinning %s to %s@%s @ %s (%d files, targets core %s)",
            domain,
            source.repo,
            source.ref,
            source.sha[:7],
            len(files),
            source.core_version,
        )
        provenance = {"git_ref": source.ref, "git_sha": source.sha, "git_repo": source.repo}
```

(the rest of the method is unchanged).

- [x] **Step 7: Accept `git_repo` over the websocket**

In `websocket.py`, import `GITHUB_REPO` from `.const`. In both the `pin` and `compare` schemas add after `git_ref`:

```python
        vol.Optional("git_repo", default=GITHUB_REPO): str,
```

`ws_pin` calls `async_pin(..., msg["acknowledge_migration"], git_repo=msg["git_repo"])`; `ws_compare` calls `async_compare(msg["domain"], msg["version"], msg["git_ref"], msg["git_repo"])`.

- [x] **Step 8: Run the whole suite**

Run: `. .venv/bin/activate && pytest -q`
Expected: all pass (60 existing + the new fork tests).

- [x] **Step 9: Commit**

```bash
git add custom_components/integration_pins/const.py custom_components/integration_pins/pinner.py custom_components/integration_pins/store.py custom_components/integration_pins/manager.py custom_components/integration_pins/websocket.py tests/test_integration_pins.py
git commit -m "feat(pins): take git pins from a github fork of core"
```

---

### Task 2: Folder uploads (backend)

**Files:**
- Create: `custom_components/integration_pins/uploads.py`
- Modify: `custom_components/integration_pins/const.py` (`SOURCE_UPLOAD`, `FEATURES`)
- Modify: `custom_components/integration_pins/store.py` (`upload_label`)
- Modify: `custom_components/integration_pins/manager.py` (staging, compare, pin)
- Modify: `custom_components/integration_pins/websocket.py` (`upload_id`)
- Modify: `custom_components/integration_pins/__init__.py` (register view)
- Test: `tests/test_integration_pins.py`

**Interfaces:**
- Consumes: `PinManager.async_compare(domain, version, git_ref, git_repo)` and `async_pin(..., git_repo=...)` from Task 1.
- Produces: `uploads.UPLOAD_URL = "/api/integration_pins/upload"`; `uploads.Upload(domain, label, files, version, created=0.0)`; `uploads.UploadStaging(clock=time.monotonic)` with `.add(upload) -> str`, `.get(upload_id) -> Upload`, `.discard(upload_id)`; `uploads.content_version(core_version, files) -> str`; `uploads.validate_upload(domain, files)`; `PinManager.uploads: UploadStaging`; `PinManager.async_stage_upload(domain, label, files) -> dict` with keys `upload_id, domain, files, version`; `async_compare(..., upload_id="")`; `async_pin(..., upload_id="")`; `Pin.upload_label: str = ""`; `const.SOURCE_UPLOAD = "upload"`. Multipart field names: `domain`, `label`, and `f:<relative path>` per file.

- [x] **Step 1: Write the failing upload tests**

Append to `tests/test_integration_pins.py` (add `import aiohttp` to the imports at the top):

```python
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
        ("", UPLOADED, "No integration domain"),
    ],
    ids=["domain_mismatch", "no_manifest", "bad_manifest", "dotdot", "absolute", "backslash", "no_domain"],
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
    assert (override / "__init__.py").read_bytes() == b"my local fix"
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
    assert (retired[0] / "__init__.py").read_bytes() == b"my local fix"
```

Also add to `test_snapshot_advertises_what_this_backend_can_do`:

```python
    assert "upload" in snap["features"]
```

- [x] **Step 2: Run the new tests to verify they fail**

Run: `. .venv/bin/activate && pytest -q -k "upload or advertises"`
Expected: FAIL — upload POSTs return 404, `ModuleNotFoundError: ... uploads`, `extra keys not allowed @ data['upload_id']`.

- [x] **Step 3: Constants and the stored field**

`const.py`: after `SOURCE_ADOPTED` add

```python
SOURCE_UPLOAD = "upload"  # a folder uploaded from the browser
```

and append `"upload",` to `FEATURES`.

`store.py`: update the `source` comment to `# "pypi", "git", "upload" or "adopted"` and add after `git_repo`:

```python
    upload_label: str = ""  # name of the folder an uploaded pin came from
```

- [x] **Step 4: Create `uploads.py`**

```python
"""Integration code uploaded from the browser.

The checkout a pin comes from may live somewhere Home Assistant cannot read -- the
docker host, say, when core runs in a container -- while the browser showing the panel
can. The panel sends the integration's directory here, and it is held in memory until
it is compared and pinned, so the two-press flow works as it does for any other source.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from aiohttp import web

from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.components.http.decorators import require_admin

from .const import DOMAIN
from .pinner import PinError

UPLOAD_URL = f"/api/{DOMAIN}/upload"
UPLOAD_TTL = 15 * 60  # seconds an upload waits to be compared or pinned
MAX_UPLOADS = 4
MAX_FILES = 2000
MAX_BYTES = 20 * 1024 * 1024
# The path travels in the field name: browsers are free to strip directories from a
# multipart filename, but a field name arrives as sent.
FILE_FIELD_PREFIX = "f:"


@dataclass
class Upload:
    """One uploaded integration directory, waiting to be compared or pinned."""

    domain: str
    label: str  # the folder's name, as picked
    files: dict[str, bytes]
    version: str
    created: float = 0.0


class UploadStaging:
    """A few recent uploads, forgotten after UPLOAD_TTL."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._uploads: dict[str, Upload] = {}

    def add(self, upload: Upload) -> str:
        self._prune()
        while len(self._uploads) >= MAX_UPLOADS:
            del self._uploads[next(iter(self._uploads))]  # oldest first: dicts keep order
        upload.created = self._clock()
        upload_id = secrets.token_urlsafe(16)
        self._uploads[upload_id] = upload
        return upload_id

    def get(self, upload_id: str) -> Upload:
        self._prune()
        upload = self._uploads.get(upload_id)
        if upload is None:
            raise PinError("That upload has expired; pick the folder again")
        return upload

    def discard(self, upload_id: str) -> None:
        self._uploads.pop(upload_id, None)

    def _prune(self) -> None:
        cutoff = self._clock() - UPLOAD_TTL
        for upload_id in [k for k, u in self._uploads.items() if u.created < cutoff]:
            del self._uploads[upload_id]


def check_path(path: str) -> str:
    """A path relative to the integration directory, or PinError."""
    if not path or path.startswith("/") or "\\" in path or ".." in path.split("/"):
        raise PinError(f"Refusing a file outside the integration's directory: {path!r}")
    return path


def _is_bytecode(path: str) -> bool:
    return "__pycache__" in path.split("/") or path.endswith(".pyc")


def validate_upload(domain: str, files: dict[str, bytes]) -> None:
    """The folder must be the integration it is being pinned as."""
    raw = files.get("manifest.json")
    if raw is None:
        raise PinError(
            "The folder has no manifest.json; pick the integration's own directory, "
            "e.g. homeassistant/components/<domain>"
        )
    try:
        manifest = json.loads(raw)
    except ValueError as err:
        raise PinError(f"manifest.json is not valid JSON: {err}") from err
    found = manifest.get("domain") if isinstance(manifest, dict) else None
    if found != domain:
        raise PinError(f"That folder is the '{found}' integration, not '{domain}'")


def content_version(core_version: str, files: dict[str, bytes]) -> str:
    """Manifest version for uploaded code: the running core plus a content hash."""
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.encode())
        digest.update(b"\0")
        digest.update(files[path])
        digest.update(b"\0")
    return f"{core_version}+local.{digest.hexdigest()[:7]}"


async def read_upload(request: web.Request) -> tuple[str, str, dict[str, bytes]]:
    """(domain, label, files) out of the panel's multipart POST, within the limits."""
    try:
        reader = await request.multipart()
    except (AssertionError, ValueError) as err:
        raise PinError("Expected a multipart upload") from err

    domain = label = ""
    files: dict[str, bytes] = {}
    total = 0
    while (part := await reader.next()) is not None:
        name = part.name or ""
        if name == "domain":
            domain = (await part.text()).strip().lower()
            continue
        if name == "label":
            label = (await part.text()).strip()[:200]
            continue
        if not name.startswith(FILE_FIELD_PREFIX):
            continue
        path = check_path(name[len(FILE_FIELD_PREFIX) :])
        if len(files) >= MAX_FILES:
            raise PinError(
                f"More than {MAX_FILES} files: pick the integration's own directory, "
                "not the whole checkout"
            )
        chunks = []
        while chunk := await part.read_chunk():
            total += len(chunk)
            if total > MAX_BYTES:
                raise PinError(f"The folder is larger than {MAX_BYTES // (1024 * 1024)} MB")
            chunks.append(chunk)
        if not _is_bytecode(path):
            files[path] = b"".join(chunks)

    if not domain:
        raise PinError("No integration domain was sent with the upload")
    return domain, label or domain, files


class UploadView(HomeAssistantView):
    """Receive an integration directory from the panel's folder picker."""

    url = UPLOAD_URL
    name = f"{DOMAIN}:upload"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        manager = request.app[KEY_HASS].data.get(DOMAIN)
        if manager is None:
            return self.json_message("Integration Pins is not loaded", 404)
        try:
            domain, label, files = await read_upload(request)
            return self.json(manager.async_stage_upload(domain, label, files))
        except PinError as err:
            return self.json_message(str(err), 400)
```

Check `KEY_HASS` is exported from `homeassistant.components.http` (it is imported at `homeassistant/components/http/__init__.py:45`). If an import fails, use `request.app["hass"]`.

- [x] **Step 5: Stage, compare and pin uploads in the manager**

`manager.py` imports: change `from . import pinner` to `from . import pinner, uploads` and add `SOURCE_UPLOAD` to the const block. In `__init__`:

```python
        self.uploads = uploads.UploadStaging()
```

Add after `async_compare` (or near the writes section):

```python
    @callback
    def async_stage_upload(self, domain: str, label: str, files: dict[str, bytes]) -> dict[str, Any]:
        """Hold an uploaded directory until it is compared and pinned."""
        domain = domain.strip().lower()
        uploads.validate_upload(domain, files)
        version = uploads.content_version(CORE_VERSION, files)
        upload_id = self.uploads.add(uploads.Upload(domain, label or domain, files, version))
        _LOGGER.info("Staged %d uploaded files from %s for %s", len(files), label, domain)
        return {"upload_id": upload_id, "domain": domain, "files": len(files), "version": version}

    def _upload_for(self, upload_id: str, domain: str) -> uploads.Upload:
        upload = self.uploads.get(upload_id)
        if upload.domain != domain:
            raise PinError(f"That upload is the '{upload.domain}' integration, not '{domain}'")
        return upload
```

Add a module-level helper near `MIGRATION_FILES`:

```python
def _one_source(version: str, git_ref: str, upload_id: str) -> None:
    if sum(bool(s) for s in (version, git_ref, upload_id)) != 1:
        raise PinError(
            "Take the code from exactly one of a release, a git ref or an uploaded folder"
        )
```

`async_compare`: add `upload_id: str = ""` as the last parameter; replace the `bool(version) == bool(git_ref)` check with `upload_id = upload_id.strip()` and `_one_source(version, git_ref, upload_id)`; add a branch before `if git_ref:` (turning that into `elif git_ref:`):

```python
        if upload_id:
            upload = self._upload_for(upload_id, domain)
            files = upload.files
            candidate = pinner.digest_files(files)
            probe = {name: files[name] for name in MIGRATION_FILES if name in files}
            label = f"upload {upload.label}"
            since = ""
```

`async_pin`: add `upload_id: str = ""` as the last parameter; replace the either/or check with `upload_id = upload_id.strip()` and `_one_source(version, git_ref, upload_id)`; replace the install branch and the `source=` argument:

```python
            if upload_id:
                pinned_version, provenance = await self._async_install_from_upload(
                    domain, upload_id, acknowledge_migration
                )
                source = SOURCE_UPLOAD
            elif git_ref:
                pinned_version, provenance = await self._async_install_from_git(
                    domain, git_ref, session, acknowledge_migration, git_repo
                )
                source = SOURCE_GIT
            else:
                pinned_version, provenance = await self._async_install_from_release(
                    domain, version, session, acknowledge_migration
                )
                source = SOURCE_PYPI
```

and `source=source,` in the `Pin(...)` call.

Add after `_async_install_from_git`:

```python
    async def _async_install_from_upload(
        self, domain: str, upload_id: str, acknowledged: bool = False
    ) -> tuple[str, dict[str, str]]:
        upload = self._upload_for(upload_id, domain)
        self._guard_migration(domain, upload.files, acknowledged)
        _LOGGER.info(
            "Pinning %s to uploaded folder %s (%d files)", domain, upload.label, len(upload.files)
        )
        provenance = {"upload_label": upload.label}
        await self.hass.async_add_executor_job(
            pinner.install_component,
            upload.files,
            domain,
            upload.version,
            self.hass.config.config_dir,
            CORE_VERSION,
            provenance,
        )
        self.uploads.discard(upload_id)
        return upload.version, provenance
```

Update the comment in `async_unpin` above `keep_a_copy` to: `# PyPI and git pins record where they came from, so the files can be fetched again; adopted and uploaded ones exist nowhere else.` (the condition itself already keeps uploads).

- [x] **Step 6: Accept `upload_id` over the websocket and register the view**

`websocket.py`: in both the `pin` and `compare` schemas add `vol.Optional("upload_id", default=""): str,`; `ws_pin` passes `upload_id=msg["upload_id"]`; `ws_compare` passes `upload_id=msg["upload_id"]`.

`__init__.py`: `from .uploads import UploadView`, and in `_async_register_panel` inside the `if not hass.data.get(_DATA_STATIC_REGISTERED):` block:

```python
        hass.http.register_view(UploadView())
```

- [x] **Step 7: Run the whole suite**

Run: `. .venv/bin/activate && pytest -q`
Expected: all pass.

- [x] **Step 8: Commit**

```bash
git add custom_components/integration_pins/uploads.py custom_components/integration_pins/const.py custom_components/integration_pins/store.py custom_components/integration_pins/manager.py custom_components/integration_pins/websocket.py custom_components/integration_pins/__init__.py tests/test_integration_pins.py
git commit -m "feat(pins): pin an integration folder uploaded from the browser"
```

---

### Task 3: Panel — repository field, folder picker, rows and links

**Files:**
- Modify: `custom_components/integration_pins/panel/integration-pins-panel.js`

**Interfaces:**
- Consumes: websocket `pin`/`compare` keys `git_repo` and `upload_id` (Tasks 1–2); `POST /api/integration_pins/upload` with fields `domain`, `label`, `f:<path>` returning `{upload_id, domain, files, version}` or `{message}` on 400; pin fields `git_repo`, `upload_label`, `source: "upload"`; features `git_repo`, `upload`.
- Produces: nothing consumed by later tasks.

There is no JS test harness in this repo; verification is `node --check` plus a manual pass in a running Home Assistant (Step 9).

- [x] **Step 1: Constants and state**

Change `PANEL_FEATURES` to:

```js
const PANEL_FEATURES = ["in_use", "domain_info", "compare", "git", "retired", "custom_integrations", "git_repo", "upload"];
const CORE_REPO = "home-assistant/core";
```

In the constructor, replace the `_source` line and add after `_gitRef`:

```js
    this._source = "release"; // "release" (a PyPI wheel), "git" (a commit in core or a fork) or "upload"
    this._gitRef = "dev";
    this._gitRepo = CORE_REPO;
    this._upload = null; // {upload_id, domain, files, version, label} staged on the backend
```

- [x] **Step 2: One place decides the effective source**

Add after `_backendHas`:

```js
  /* The selected source, falling back to a release when the backend cannot do it. */
  _effectiveSource() {
    if (this._source === "git" && this._backendHas("git")) return "git";
    if (this._source === "upload" && this._backendHas("upload")) return "upload";
    return "release";
  }

  /* Where history and commit links point: the fork being pinned from, else core. */
  _linkRepo() {
    return this._effectiveSource() === "git" ? this._gitRepo.trim() || CORE_REPO : CORE_REPO;
  }
```

- [x] **Step 3: `_formSource` and `_candidateKey` understand all three sources**

Replace `_formSource` and `_candidateKey` with:

```js
  _formSource(form) {
    const src = this._effectiveSource();
    const domain = (form?.domain?.value || "").trim().toLowerCase();
    const ref = (form?.git_ref?.value || "").trim();
    const repo = (form?.git_repo?.value || "").trim() || CORE_REPO;
    const version = (form?.version?.value || "").trim();
    this._fieldErrors = {};
    if (!domain) this._fieldErrors.domain = "Pick an integration first";
    if (src === "git" && !ref) this._fieldErrors.git_ref = "Enter a branch, tag or commit";
    if (src === "release" && !version) this._fieldErrors.version = "Pick a release first";
    if (src === "upload" && !this._upload) this._fieldErrors.folder = "Pick the integration's folder first";
    if (src === "upload" && this._upload && domain && this._upload.domain !== domain)
      this._fieldErrors.folder = `That folder is ${this._upload.domain}, not ${domain}`;
    if (Object.keys(this._fieldErrors).length) {
      this._paintFieldErrors();
      return null;
    }
    if (src === "git") {
      const label = repo === CORE_REPO ? ref : `${repo}@${ref}`;
      const payload = this._backendHas("git_repo") ? { git_ref: ref, git_repo: repo } : { git_ref: ref };
      return { domain, payload, label, busy: `Fetching ${domain} from ${label}…` };
    }
    if (src === "upload") {
      return {
        domain,
        payload: { upload_id: this._upload.upload_id },
        label: `upload ${this._upload.label}`,
        busy: `Checking ${this._upload.label}…`,
      };
    }
    return {
      domain,
      payload: { version },
      label: version,
      busy: `Downloading Home Assistant ${version} and extracting ${domain}… this can take a minute.`,
    };
  }

  _candidateKey(chosen) {
    return `${this._source}|${chosen.domain}|${chosen.label}|${chosen.payload.upload_id || ""}`;
  }
```

- [x] **Step 4: Upload the folder as soon as it is picked**

The file input has no `name` on purpose: `_render` restores named fields from `FormData`, and assigning a value to a file input throws. It is found by `data-role="folder"` instead, and the staged upload lives in `this._upload`, so re-renders lose nothing.

At the top of `_onChange(ev)` add:

```js
    if (ev.target.dataset?.role === "folder") return this._uploadFolder(ev.target.files);
```

Add the method after `_runCompare`:

```js
  /* webkitRelativePath starts with the picked folder's own name; the backend wants paths
   * relative to the integration directory. Checked here first so picking the whole
   * checkout fails at once instead of after uploading a hundred thousand files. */
  async _uploadFolder(fileList) {
    const files = [...(fileList || [])];
    this._upload = null;
    this._compare = null;
    this._comparedKey = null;
    this._pinNeedsConfirm = false;
    this._migrationBlocked = false;
    this._ackMigration = false;
    this._fieldErrors = {};
    if (!files.length) return this._render();
    const fail = (message) => {
      this._fieldErrors = { folder: message };
      this._render();
    };
    const label = files[0].webkitRelativePath.split("/")[0];
    const entries = files
      .map((file) => ({ file, path: file.webkitRelativePath.split("/").slice(1).join("/") }))
      .filter(({ path }) => path && !path.split("/").includes("__pycache__") && !path.endsWith(".pyc"));
    const manifest = entries.find((e) => e.path === "manifest.json");
    if (!manifest) return fail(`${label} has no manifest.json; pick the integration's own directory, not the checkout`);
    let domain = "";
    try {
      domain = String(JSON.parse(await manifest.file.text()).domain || "");
    } catch (e) {
      return fail(`${label}/manifest.json is not valid JSON`);
    }
    const form = this.shadowRoot.querySelector("#add-form");
    const typed = (form?.domain?.value || "").trim().toLowerCase();
    if (typed && typed !== domain) return fail(`${label} is the ${domain} integration, not ${typed}`);
    if (!typed && form?.domain) {
      form.domain.value = domain;
      form.domain.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    }
    const body = new FormData();
    body.append("domain", domain);
    body.append("label", label);
    for (const { file, path } of entries) body.append(`f:${path}`, file, file.name);
    this._error = null;
    this._busy = `Uploading ${entries.length} files from ${label}…`;
    this._render();
    try {
      const resp = await this._hass.fetchWithAuth("/api/integration_pins/upload", { method: "POST", body });
      const r = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(r.message || `HTTP ${resp.status}`);
      this._upload = { ...r, label };
    } catch (e) {
      this._fieldErrors = { folder: `Upload failed: ${e.message || e}` };
    } finally {
      this._busy = null;
      this._render();
    }
  }
```

In `_pin` and `_runCompare`, right after the `const r = await this._call(...)`, drop a staged upload the backend no longer has:

```js
    if (!r && /pick the folder again/.test(this._error || "")) this._upload = null;
```

In `_resetAddForm` add `this._gitRepo = CORE_REPO;` and `this._upload = null;`.

In `_onInput`, handle the repository like the ref (add before the `git_ref` branch):

```js
    if (ev.target.name === "git_repo") {
      this._gitRepo = ev.target.value;
      this._comparedKey = null;
      this._pinNeedsConfirm = false;
      this._clearFieldError("git_repo");
      this._paintDomainAids();
      return;
    }
```

- [x] **Step 5: The form**

In `_addCard`, replace the `canGit` / `git` lines with:

```js
    const canGit = this._backendHas("git");
    const canUpload = this._backendHas("upload");
    const src = this._effectiveSource();
```

and replace the whole "Take code from" `<label>` with:

```js
            <label>
              <span class="label-row">Take code from${canGit || canUpload ? `
                <span class="segmented">
                  <button type="button" data-action="source" data-source="release" class="${src === "release" ? "on" : ""}">a release</button>
                  ${canGit ? `<button type="button" data-action="source" data-source="git" class="${src === "git" ? "on" : ""}">git</button>` : ""}
                  ${canUpload ? `<button type="button" data-action="source" data-source="upload" class="${src === "upload" ? "on" : ""}">a folder</button>` : ""}
                </span>` : ""}
              </span>
              ${src === "git" ? `
                ${this._backendHas("git_repo") ? `
                <span class="control">
                  <input name="git_repo" value="${esc(this._gitRepo)}" placeholder="${CORE_REPO}" autocomplete="off" class="mono">
                  <span class="field-error" data-for="git_repo" hidden></span>
                </span>` : ""}
                <span class="control">
                  <input name="git_ref" value="${esc(this._gitRef)}" placeholder="dev" autocomplete="off" class="mono">
                  <span class="field-error" data-for="git_ref" hidden></span>
                </span>
                <span class="small muted">GitHub repository (core or a fork of it), then a branch, tag or commit</span>
              ` : src === "upload" ? `
                <span class="control">
                  <input type="file" data-role="folder" webkitdirectory>
                  <span class="field-error" data-for="folder" hidden></span>
                </span>
                <span class="small muted">${this._upload
                  ? `${esc(this._upload.label)}: ${esc(this._upload.files)} files, staged as <span class="mono">${esc(this._upload.version)}</span>`
                  : "the integration's own directory, e.g. homeassistant/components/&lt;domain&gt;"}</span>
              ` : `
                <span class="control">
                  <select name="version" class="mono">
                    <option value="">${this._versions.length ? "select…" : "loading from PyPI…"}</option>${opts}
                  </select>
                  <span class="field-error" data-for="version" hidden></span>
                </span>
                <span class="small"><input type="checkbox" name="prereleases" ${this._prereleases ? "checked" : ""}> include betas</span>
              `}
            </label>
```

- [x] **Step 6: Source warning and links**

Replace `_sourceWarningHtml` with:

```js
  _sourceWarningHtml() {
    const src = this._effectiveSource();
    if (src === "git") {
      return `
      <div class="warn-box">
        <ha-icon icon="mdi:source-branch"></ha-icon>
        <div>
          Git code targets the <strong>next</strong> core release, not the one you are running.
          Pinning an older release is protected by core's deprecation policy; pinning ahead of your
          core is not, so the code can call helpers your core does not have yet.
          The repository also carries English translations only — Home Assistant generates the rest
          at release time — so a git pin falls back to English everywhere else.
        </div>
      </div>`;
    }
    if (src === "upload") {
      return `
      <div class="warn-box">
        <ha-icon icon="mdi:folder-upload"></ha-icon>
        <div>
          An uploaded folder is whatever it holds right now; nothing checks it beyond the comparison below.
          A checkout of core's <code>dev</code> targets the <strong>next</strong> core release, and carries
          English translations only unless you built it. No other copy exists, so unpinning moves it to
          <code>integration_pins_retired/</code> instead of deleting it.
        </div>
      </div>`;
    }
    return "";
  }
```

Make `_historyUrl` take the repository:

```js
  _historyUrl(path, ref = "dev", since = "", repo = CORE_REPO) {
    const base = `https://github.com/${repo}/commits`;
    const encoded = path.split("/").map(encodeURIComponent).join("/");
    const query = since ? `?since=${encodeURIComponent(since)}` : "";
    return `${base}/${encodeURIComponent(ref)}/${encoded}${query}`;
  }

  _historyLink(path, text, ref = "dev", since = "", repo = CORE_REPO) {
    return `<a href="${this._historyUrl(path, ref, since, repo)}" target="_blank" rel="noopener noreferrer">${esc(text)}</a>`;
  }
```

In `_domainLinksHtml`, replace the `version` and first `links` lines with:

```js
    const src = this._effectiveSource();
    const repo = this._linkRepo();
    const version = src === "release" ? this.shadowRoot.querySelector("#add-form")?.version?.value || "" : "";
    const since = version ? this._released[version] || "" : "";
    const latestRef = repo === CORE_REPO ? "dev" : this._gitRef.trim() || "dev";
    const links = [this._historyLink(path, "latest", latestRef, "", repo)];
```

In the compare file list (currently line ~294), pass the repo so a fork's files link into the fork:

```js
          .map((f) => this._historyLink(`homeassistant/components/${c.domain}/${f}`, f, this._linkRepo() === CORE_REPO ? "dev" : this._gitRef.trim() || "dev", c.since, this._linkRepo()))
```

In the `case "source":` click handler add `this._upload = null;` alongside the other resets.

- [x] **Step 7: Pin rows, unpin text and re-pin**

In `_pinRow`, replace the domain and version cells:

```js
        <div class="cell"><b>${esc(p.domain)}</b>${p.source === "adopted" || p.source === "upload" ? ` <span class="tag">${esc(p.source)}</span>` : ""}</div>
        <div class="cell mono">${esc(p.pinned_version)}${p.source === "git" ? `<div class="small">${this._gitOrigin(p)}</div>` : ""}${p.source === "upload" ? `<div class="small">from ${esc(p.upload_label)}</div>` : ""}</div>
```

and only offer Re-pin where there is somewhere to fetch from:

```js
        ${(p.status === "missing" || p.status === "foreign") && p.source !== "upload" ? `<ha-button data-action="repin" data-domain="${esc(p.domain)}" ${this._busy ? "disabled" : ""}>Re-pin</ha-button>` : ""}
```

Replace `_gitOrigin`:

```js
  _gitOrigin(p) {
    const repo = p.git_repo || CORE_REPO;
    const commit = `https://github.com/${repo}/commit/${encodeURIComponent(p.git_sha)}`;
    const where = repo === CORE_REPO ? "" : `${esc(repo)} `;
    return `<span class="tag">git</span> ${where}${esc(p.git_ref)} @ <a href="${commit}" target="_blank" rel="noopener noreferrer">${esc((p.git_sha || "").slice(0, 7))}</a>`;
  }
```

Replace `_repin` so a git pin refetches its recorded commit instead of asking PyPI for a `+sha` version:

```js
  async _repin(pin) {
    const git = pin.source === "git";
    const source = git
      ? { git_ref: pin.git_sha, ...(this._backendHas("git_repo") ? { git_repo: pin.git_repo || CORE_REPO } : {}) }
      : { version: pin.pinned_version };
    const from = git ? `${pin.git_repo || CORE_REPO} @ ${(pin.git_sha || "").slice(0, 7)}` : `Home Assistant ${pin.pinned_version}`;
    const r = await this._call(
      { type: "integration_pins/pin", domain: pin.domain, ...source, core_range: pin.core_range, reason: pin.reason },
      `Re-fetching ${pin.domain} from ${from}…`
    );
    if (r) this._notice = `Restored ${pin.domain} from ${from}. Restart to load it.`;
    this._render();
  }
```

In `_unpin`, change the git wording to name the repository:

```js
      ? `custom_components/${domain}/ is deleted; it came from ${pin.source === "git" ? `a commit in ${pin.git_repo || CORE_REPO}` : "a release on PyPI"}, so re-pinning fetches it again.`
```

- [x] **Step 8: Syntax check**

Run: `node --check custom_components/integration_pins/panel/integration-pins-panel.js`
Expected: no output, exit 0. Also `grep -n "canGit && this._source" custom_components/integration_pins/panel/integration-pins-panel.js` — expected no matches (every source decision goes through `_effectiveSource`).

- [x] **Step 9: Manual check in a running Home Assistant**

With this checkout's `custom_components/integration_pins` loaded in a dev Home Assistant (restart for the Python, reload the browser for the panel), confirm:

1. The git source shows a Repository field prefilled `home-assistant/core`; entering a fork and a ref, Compare labels the result `owner/core@ref @ sha7`, and the history links open the fork.
2. The folder source: picking `…/homeassistant/components/sun` fills the domain, shows the staged file count and version; Compare then Pin works; the row shows the `upload` tag and `from sun`, with no Re-pin.
3. Picking a checkout root shows "has no manifest.json … not the checkout" without uploading.
4. Unpinning the upload pin moves it to the retired list.

If no dev instance is available, report this step as not done instead of claiming it.

_Done 2026-09-22 against a throwaway instance on :18123 driven by headless Playwright; a real fork was not exercised in the browser (backend tests cover it)._

- [x] **Step 10: Commit**

```bash
git add custom_components/integration_pins/panel/integration-pins-panel.js
git commit -m "feat(panel): pick a fork or upload a folder as the pin's source"
```

---

### Task 4: README and wrap-up

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/plans/2026-09-22-arbitrary-sources.md` (tick boxes)

- [x] **Step 1: Document the sources**

In `README.md` under "Using it", after the "Take the code from git" paragraph, add (each paragraph one line, no hard wraps):

```markdown
**From a fork** — the git option also takes a GitHub repository, `home-assistant/core` by default. Any fork of core laid out the same way works, so a fix in someone's fork can be pinned before it is merged; a pasted `https://github.com/owner/core` URL is fine.

**From a folder** — pick an integration's directory from a checkout on the machine your browser runs on, e.g. `homeassistant/components/zha`, and the panel uploads it. Pick the integration's own directory, not the whole checkout. Home Assistant never needs to reach that machine, which is what makes this work when core runs in a container and the checkout lives on the host. The upload is held for 15 minutes for Compare and Pin. It has no other copy, so unpinning keeps it in `integration_pins_retired/` and there is no Re-pin; the version is your core's plus a hash of the files, like `2026.9.1+local.3fa94c1`.
```

In "Development", add `  uploads.py        folder uploads: HTTP view, validation, in-memory staging` to the module list after `pinner.py`.

- [x] **Step 2: Full test run**

Run: `. .venv/bin/activate && pytest -q`
Expected: all pass.

- [x] **Step 3: Commit**

```bash
git add README.md docs/superpowers/plans/2026-09-22-arbitrary-sources.md
git commit -m "docs(readme): describe pinning from forks and uploaded folders"
```

- [x] **Step 4: Release the agent claim**

```bash
rm "$(git rev-parse --git-common-dir)/agent-claims/arbitrary-sources.md"
```
