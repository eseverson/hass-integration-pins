"""Orchestrates pins: store + filesystem + repairs."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant import loader
from homeassistant.const import __version__ as CORE_VERSION
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later

from . import pinner, uploads
from .const import (
    DOMAIN,
    FEATURES,
    GITHUB_REPO,
    MARKER_FILE,
    OVERRIDE_DELETED,
    OVERRIDE_LEFT,
    OVERRIDE_RETIRED,
    RETIRED_DIRNAME,
    SOURCE_GIT,
    SOURCE_PYPI,
    SOURCE_UPLOAD,
    ISSUE_FOREIGN,
    ISSUE_MISSING,
    ISSUE_OUT_OF_RANGE,
    STATUS_ACTIVE,
    STATUS_FOREIGN,
    STATUS_MISSING,
    STATUS_OUT_OF_RANGE,
    STATUS_PENDING,
)
from .pinner import ForeignDirectoryNotConfirmed, MigrationBlocked, PinError
from .store import Pin, PinStore

_LOGGER = logging.getLogger(__name__)

SIGNAL_UPDATED = f"{DOMAIN}_updated"

# The files that say what pinning would do to a stored config entry: the flow declares
# the schema version, and the component declares whether it can migrate one.
MIGRATION_FILES = ["config_flow.py", "__init__.py"]


def _one_source(version: str, git_ref: str, upload_id: str) -> None:
    if sum(bool(s) for s in (version, git_ref, upload_id)) != 1:
        raise PinError(
            "Take the code from exactly one of a release, a git ref or an uploaded folder"
        )


class PinManager:
    """Single instance stored in hass.data[DOMAIN]."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.store = PinStore(hass)
        # Set when files were removed; pending pins are derived from the loader instead.
        self._removal_pending = False
        self._dependents: dict[str, list[str]] | None = None
        self._lock = asyncio.Lock()
        self.uploads = uploads.UploadStaging()
        self._cancel_upload_sweep: CALLBACK_TYPE | None = None
        self._listeners: list[callback] = []

    # -- lifecycle -----------------------------------------------------------

    async def async_setup(self) -> None:
        await self.store.async_load()
        await self.async_check()

    @callback
    def async_unload(self) -> None:
        if self._cancel_upload_sweep:
            self._cancel_upload_sweep()
            self._cancel_upload_sweep = None

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
        core_domains = set(await self.async_core_domains())
        custom = self._custom_integrations(loaded, core_domains | set(self.store.pins) | {DOMAIN})
        retired = await self.hass.async_add_executor_job(pinner.list_retired, config_dir)
        return {
            "core_version": CORE_VERSION,
            "features": list(FEATURES),
            "restart_required": self._removal_pending
            or any(p["status"] == STATUS_PENDING for p in pins),
            "pins": pins,
            "unmanaged_overrides": unmanaged,
            "custom_integrations": custom,
            "retired": retired,
        }

    @staticmethod
    def _custom_integrations(
        loaded: dict[str, loader.Integration], exclude: set[str]
    ) -> list[dict[str, Any]]:
        """Third-party integrations that do not shadow a core domain, as the loader found
        them at startup, rather than read out of their own directories."""
        return [
            {
                "domain": domain,
                "name": integration.name,
                "version": str(integration.version) if integration.version else None,
            }
            for domain, integration in sorted(loaded.items())
            if domain not in exclude
        ]

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

    async def async_compare(
        self,
        domain: str,
        version: str = "",
        git_ref: str = "",
        git_repo: str = GITHUB_REPO,
        upload_id: str = "",
    ) -> dict[str, Any]:
        """Which files of an integration differ between a candidate and the running code."""
        domain = domain.strip().lower()
        version = version.strip()
        git_ref = git_ref.strip()
        upload_id = upload_id.strip()
        _one_source(version, git_ref, upload_id)
        session = async_get_clientsession(self.hass)

        if upload_id:
            upload = self._upload_for(upload_id, domain)
            files = upload.files
            candidate = pinner.digest_files(files)
            probe = {name: files[name] for name in MIGRATION_FILES if name in files}
            label = f"upload {upload.label}"
            since = ""
        elif git_ref:
            source = await pinner.async_resolve_git_source(session, git_ref, git_repo)
            files = await pinner.async_git_component(session, source, domain)
            candidate = pinner.digest_files(files)
            probe = {name: files[name] for name in MIGRATION_FILES if name in files}
            ref = source.ref if source.repo == GITHUB_REPO else f"{source.repo}@{source.ref}"
            label = f"{ref} @ {source.sha[:7]}"
            since = source.date
        else:
            pinner.parse_version(version)
            wheel = await pinner.async_get_wheel(session, version)
            entries = await pinner.async_central_directory(session, wheel)
            candidate = pinner.component_digest(entries, domain)
            if not candidate:
                raise PinError(f"Home Assistant {version} has no core integration '{domain}'")
            probe = await pinner.async_read_entries(
                session, wheel, entries, domain, MIGRATION_FILES
            )
            label = version
            since = wheel.released

        base, compared_with = await self._async_baseline(domain, session)
        result = pinner.compare_digests(base, candidate)
        foreign = await self._async_foreign_override(domain)
        return {
            **result,
            "replaces_foreign": foreign is not None,
            "foreign": foreign,
            "domain": domain,
            "version": label,
            "compared_with": compared_with,
            "since": since,
            "migration": self.async_migration_check(domain, probe),
            "identical": not (result["added"] or result["removed"] or result["changed"]),
        }

    async def async_stage_upload(
        self, domain: str, label: str, files: dict[str, bytes]
    ) -> dict[str, Any]:
        """Hold an uploaded directory until it is compared and pinned."""
        domain = domain.strip().lower()
        uploads.validate_upload(domain, files)
        # Up to MAX_BYTES of content to hash: the executor's job, not the event loop's.
        version = await self.hass.async_add_executor_job(
            uploads.content_version, CORE_VERSION, files
        )
        upload_id = self.uploads.add(uploads.Upload(domain, label or domain, files, version))
        self._async_schedule_upload_sweep()
        _LOGGER.info("Staged %d uploaded files from %s for %s", len(files), label, domain)
        return {"upload_id": upload_id, "domain": domain, "files": len(files), "version": version}

    @callback
    def _async_schedule_upload_sweep(self, _now: Any = None) -> None:
        """Free each upload when it expires, rather than on the next upload or pin."""
        if self._cancel_upload_sweep:
            self._cancel_upload_sweep()
        delay = self.uploads.prune()
        self._cancel_upload_sweep = (
            None
            if delay is None
            else async_call_later(self.hass, delay, self._async_schedule_upload_sweep)
        )

    def _upload_for(self, upload_id: str, domain: str) -> uploads.Upload:
        upload = self.uploads.get(upload_id)
        if upload.domain != domain:
            raise PinError(f"That upload is the '{upload.domain}' integration, not '{domain}'")
        return upload

    async def _async_baseline(
        self, domain: str, session: Any
    ) -> tuple[dict[str, tuple[int, int]], str]:
        """The digest of whatever code this instance is running for `domain`, and its name."""
        config_dir = self.hass.config.config_dir
        info = await self.hass.async_add_executor_job(pinner.inspect_override, domain, config_dir)
        pin = self.store.get(domain)
        marker = info.get("marker") or {}

        managed = (
            info.get("present")
            and pin is not None
            and marker.get("pinned_version") == pin.pinned_version
        )
        # An override we wrote is not byte-identical to its source -- pinning rewrites the
        # manifest and adds a marker -- so the honest baseline is the source it came from.
        if managed and pin.source == SOURCE_PYPI:
            base_wheel = await pinner.async_get_wheel(session, pin.pinned_version)
            entries = await pinner.async_component_entries(session, base_wheel, domain)
            if entries:
                return entries, f"pinned {pin.pinned_version}"
        if managed and pin.source == SOURCE_GIT and pin.git_sha:
            source = pinner.GitSource(
                pin.git_ref, pin.git_sha, pin.core_at_pin, repo=pin.git_repo
            )
            files = await pinner.async_git_component(session, source, domain)
            return pinner.digest_files(files), f"pinned {pin.pinned_version}"

        if info.get("present"):
            path = pinner.custom_components_dir(config_dir) / domain
            digest = await self.hass.async_add_executor_job(
                pinner.local_component_digest, path, {MARKER_FILE}
            )
            return digest, f"custom_components/{domain}"

        path = pinner.core_components_dir() / domain
        digest = await self.hass.async_add_executor_job(pinner.local_component_digest, path)
        return digest, f"core {CORE_VERSION}"

    async def _async_foreign_override(self, domain: str) -> dict[str, str] | None:
        """What is in custom_components/<domain>, if pinning would replace code we did not write.

        None when the directory is empty or is the override a pin of ours wrote. The name
        and version come from what the loader found at startup, falling back to the
        manifest version the panel already reads for overrides.
        """
        info = await self.hass.async_add_executor_job(
            pinner.inspect_override, domain, self.hass.config.config_dir
        )
        if not info.get("present"):
            return None
        pin = self.store.get(domain)
        if pin is not None and pinner.marker_matches(info.get("marker"), pin.pinned_version):
            return None
        integration = (await loader.async_get_custom_components(self.hass)).get(domain)
        name = integration.name if integration else ""
        version = integration.version if integration and integration.version else None
        return {
            "domain": domain,
            "name": name,
            "version": str(version or info.get("manifest_version") or ""),
            "retired_dir": RETIRED_DIRNAME,
        }

    @callback
    def async_migration_check(self, domain: str, files: dict[str, bytes]) -> dict[str, Any]:
        """What pinning this code would do to the config entry already stored.

        Home Assistant compares the stored entry's version with the ConfigFlow's. Equal
        majors are fine. A higher one in the code runs async_migrate_entry, which rewrites
        the entry and is not undone by unpinning. A lower one either finds no migration
        handler and fails outright, or hands a newer entry to a migration written to move
        forward, which almost always fails too.
        """
        markers = pinner.migration_markers(files)
        source = files.get("config_flow.py")
        code = pinner.config_entry_schema(source.decode("utf-8", "replace")) if source else None
        entries = self.hass.config_entries.async_entries(domain)
        stored = max(((e.version, e.minor_version) for e in entries), default=None)

        result = {
            "stored": list(stored) if stored else None,
            "code": list(code) if code else None,
            "unique_id_migration": markers["unique_id"],
            "has_migrate_handler": markers["config_entry"],
        }
        if code is None:
            return {**result, "verdict": "unknown"}
        if stored is None:
            return {**result, "verdict": "no_entry"}
        if stored == code:
            return {**result, "verdict": "same"}
        if stored[0] == code[0]:
            return {**result, "verdict": "minor"}
        if code[0] > stored[0]:
            return {**result, "verdict": "forward"}
        return {**result, "verdict": "downgrade" if markers["config_entry"] else "blocked"}

    @callback
    def _guard_migration(
        self, domain: str, files: dict[str, bytes], acknowledged: bool
    ) -> dict[str, Any]:
        check = self.async_migration_check(domain, files)
        if check["verdict"] == "blocked" and not acknowledged:
            stored, code = check["stored"], check["code"]
            raise MigrationBlocked(
                f"This code expects config entry version {code[0]} but '{domain}' has "
                f"stored version {stored[0]}, and the code has no migration handler, so "
                "Home Assistant will refuse to set it up. Tick the box to pin it anyway."
            )
        return check

    async def async_versions(self, include_prereleases: bool) -> dict[str, str]:
        session = async_get_clientsession(self.hass)
        return await pinner.async_list_versions(session, include_prereleases)

    @staticmethod
    def _status_for(
        pin: Pin, info: dict[str, Any], integration: loader.Integration | None
    ) -> str:
        """Classify a pin. `integration` is the custom integration Home Assistant loaded."""
        if not info.get("present"):
            return STATUS_MISSING
        if not pinner.marker_matches(info.get("marker"), pin.pinned_version):
            return STATUS_FOREIGN
        # Custom integrations are scanned once at startup, so an override written since
        # then is on disk but not running yet -- as is a re-pin over a loaded override.
        if integration is None or str(integration.version) != pin.pinned_version:
            return STATUS_PENDING
        if not pinner.version_in_range(CORE_VERSION, pin.core_range):
            return STATUS_OUT_OF_RANGE
        return STATUS_ACTIVE

    # -- writes --------------------------------------------------------------

    async def async_pin(
        self,
        domain: str,
        version: str = "",
        core_range: str = "",
        reason: str = "",
        git_ref: str = "",
        acknowledge_migration: bool = False,
        git_repo: str = GITHUB_REPO,
        upload_id: str = "",
        confirm_replace_foreign: bool = False,
    ) -> Pin:
        domain = domain.strip().lower()
        version = version.strip()
        git_ref = git_ref.strip()
        upload_id = upload_id.strip()
        _one_source(version, git_ref, upload_id)
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
            # Before anything is fetched: replacing someone else's directory is the user's call.
            if not confirm_replace_foreign and (
                foreign := await self._async_foreign_override(domain)
            ):
                raise ForeignDirectoryNotConfirmed(foreign)
            session = async_get_clientsession(self.hass)
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
                if (
                    existing
                    and existing.source == SOURCE_GIT
                    and existing.git_sha == provenance["git_sha"] == git_ref
                    and existing.git_repo == provenance["git_repo"]
                ):
                    # Re-pinning at the recorded commit, as the panel's Re-pin does: keep
                    # the branch or tag it was asked for rather than showing the sha twice.
                    provenance["git_ref"] = existing.git_ref
            else:
                pinned_version, provenance = await self._async_install_from_release(
                    domain, version, session, acknowledge_migration
                )
                source = SOURCE_PYPI
            pin = Pin(
                domain=domain,
                pinned_version=pinned_version,
                core_range=core_range.strip(),
                reason=reason.strip(),
                core_at_pin=CORE_VERSION,
                source=source,
                created=existing.created if existing else Pin.__dataclass_fields__["created"].default_factory(),
                **provenance,
            )
            await self.store.async_set(pin)
        await self.async_check()
        return pin

    async def _async_install_from_release(
        self, domain: str, version: str, session: Any, acknowledged: bool = False
    ) -> tuple[str, dict[str, str]]:
        pinner.parse_version(version)
        wheel = await pinner.async_get_wheel(session, version)
        # Checked from the wheel's index before the body is fetched, so a pin that cannot
        # work is refused for a few kilobytes rather than fifty megabytes.
        self._guard_migration(
            domain,
            await pinner.async_read_wheel_files(session, wheel, domain, MIGRATION_FILES),
            acknowledged,
        )
        _LOGGER.info(
            "Pinning %s to Home Assistant %s (%.1f MB wheel)", domain, version, wheel.size / 1e6
        )
        config_dir = self.hass.config.config_dir
        tmp = await self.hass.async_add_executor_job(pinner.make_download_dir, config_dir)
        try:
            wheel_path = tmp / f"homeassistant-{version}.whl"
            await pinner.async_download(self.hass, session, wheel, wheel_path)
            await self.hass.async_add_executor_job(
                pinner.extract_component, wheel_path, domain, version, config_dir, CORE_VERSION
            )
        finally:
            await self.hass.async_add_executor_job(pinner.remove_download_dir, tmp)
        return version, {}

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
        await self.hass.async_add_executor_job(
            pinner.install_component,
            files,
            domain,
            source.version,
            self.hass.config.config_dir,
            CORE_VERSION,
            provenance,
        )
        return source.version, provenance

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

    async def async_unpin(self, domain: str, keep_files: bool = False) -> str:
        """Remove a pin, and its override if that is still the code it wrote.

        Returns what happened to custom_components/<domain>, as an OVERRIDE_* value.
        """
        pin = self.store.get(domain)
        if pin is None:
            raise PinError(f"No pin for '{domain}'")
        outcome = OVERRIDE_LEFT
        async with self._lock:
            if not keep_files:
                # PyPI and git pins record where they came from, so the files can be fetched
                # again; adopted and uploaded ones exist nowhere else.
                keep_a_copy = pin.source not in (SOURCE_PYPI, SOURCE_GIT)
                outcome, retired = await self.hass.async_add_executor_job(
                    pinner.remove_override,
                    domain,
                    pin.pinned_version,
                    self.hass.config.config_dir,
                    keep_a_copy,
                )
                if outcome == OVERRIDE_RETIRED:
                    _LOGGER.info("Retired override for %s to %s", domain, retired)
                elif outcome == OVERRIDE_DELETED:
                    _LOGGER.info("Deleted override for %s (re-pinnable from its source)", domain)
                elif outcome == OVERRIDE_LEFT:
                    _LOGGER.info(
                        "custom_components/%s is not the code this pin wrote; left it in place",
                        domain,
                    )
                if outcome != OVERRIDE_LEFT:
                    self._removal_pending = True
            await self.store.async_remove(domain)
        for issue in (ISSUE_OUT_OF_RANGE, ISSUE_MISSING, ISSUE_FOREIGN):
            ir.async_delete_issue(self.hass, DOMAIN, f"{issue}_{domain}")
        await self.async_check()
        return outcome

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
