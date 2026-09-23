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
