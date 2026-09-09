"""Fetch integration code from a Home Assistant release and install it as an override.

All blocking filesystem work lives in plain functions that the manager runs in the
executor. Network access goes through Home Assistant's shared aiohttp session.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import struct
import tempfile
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from homeassistant.exceptions import HomeAssistantError

from .const import MARKER_FILE, PYPI_JSON_URL, PYPI_VERSION_URL, RETIRED_DIRNAME

_LOGGER = logging.getLogger(__name__)

_MIN_LISTED_VERSION = Version("2024.1.0")


class PinError(HomeAssistantError):
    """Raised for user-facing pin failures."""


def parse_version(text: str) -> Version:
    try:
        return Version(text)
    except InvalidVersion as err:
        raise PinError(f"'{text}' is not a valid Home Assistant version") from err


def parse_range(text: str) -> SpecifierSet:
    """Validate a PEP 440 specifier set. Empty string means 'any'."""
    text = (text or "").strip()
    try:
        return SpecifierSet(text, prereleases=True)
    except InvalidSpecifier as err:
        raise PinError(
            f"'{text}' is not a valid version range (examples: ==2026.9.1, "
            "~=2026.9.0, >=2026.9.0,<2026.11.0)"
        ) from err


def version_in_range(version: str, core_range: str) -> bool:
    spec = parse_range(core_range)
    return parse_version(version) in spec


@dataclass
class WheelInfo:
    version: str
    url: str
    sha256: str
    size: int


# ---------------------------------------------------------------------------
# PyPI
# ---------------------------------------------------------------------------


async def async_list_versions(
    session: aiohttp.ClientSession, include_prereleases: bool = False
) -> list[str]:
    """Return Home Assistant release versions available on PyPI, newest first."""
    async with session.get(PYPI_JSON_URL, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        if resp.status != 200:
            raise PinError(f"PyPI returned HTTP {resp.status} listing releases")
        data = await resp.json()

    versions: list[Version] = []
    for raw, files in data.get("releases", {}).items():
        try:
            ver = Version(raw)
        except InvalidVersion:
            continue
        if ver < _MIN_LISTED_VERSION:
            continue
        if ver.is_prerelease and not include_prereleases:
            continue
        if not any(f.get("packagetype") == "bdist_wheel" and not f.get("yanked") for f in files):
            continue
        versions.append(ver)
    return [str(v) for v in sorted(versions, reverse=True)]


async def async_get_wheel(session: aiohttp.ClientSession, version: str) -> WheelInfo:
    url = PYPI_VERSION_URL.format(version=version)
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        if resp.status == 404:
            raise PinError(f"Home Assistant {version} does not exist on PyPI")
        if resp.status != 200:
            raise PinError(f"PyPI returned HTTP {resp.status} for {version}")
        data = await resp.json()

    for file in data.get("urls", []):
        if file.get("packagetype") == "bdist_wheel" and not file.get("yanked"):
            return WheelInfo(
                version=version,
                url=file["url"],
                sha256=file.get("digests", {}).get("sha256", ""),
                size=int(file.get("size", 0)),
            )
    raise PinError(f"No wheel found on PyPI for Home Assistant {version}")


async def async_download(session: aiohttp.ClientSession, wheel: WheelInfo, dest: Path) -> None:
    """Stream the wheel to disk and verify its digest."""
    digest = hashlib.sha256()
    async with session.get(wheel.url, timeout=aiohttp.ClientTimeout(total=600)) as resp:
        if resp.status != 200:
            raise PinError(f"Download failed with HTTP {resp.status}")
        with dest.open("wb") as fh:
            async for chunk in resp.content.iter_chunked(1 << 20):
                fh.write(chunk)
                digest.update(chunk)
    if wheel.sha256 and digest.hexdigest() != wheel.sha256:
        dest.unlink(missing_ok=True)
        raise PinError("Downloaded wheel failed sha256 verification")


# ---------------------------------------------------------------------------
# Comparing a release against the running code
#
# A wheel is a zip, and a zip's central directory carries the CRC32 and size of
# every member. Range-fetching that directory -- under a tenth of the file -- is
# enough to say exactly which files of an integration differ, without pulling the
# 50 MB body. pip extracts wheel members verbatim, so the CRC32 of an installed
# core file equals the one recorded in the wheel it came from.
# ---------------------------------------------------------------------------

_EOCD_SIG = b"PK\x05\x06"
_ZIP64_EOCD_SIG = b"PK\x06\x06"
_CD_ENTRY_SIG = b"PK\x01\x02"
_TAIL_BYTES = 1 << 20  # enough for the end-of-directory records plus any comment


def parse_central_directory(data: bytes) -> dict[str, tuple[int, int]]:
    """Map member name -> (crc32, uncompressed size) from raw central directory bytes.

    Accepts a whole zip as well: parsing starts at the first entry signature.
    """
    entries: dict[str, tuple[int, int]] = {}
    pos = data.find(_CD_ENTRY_SIG)
    if pos < 0:
        return entries
    while pos + 46 <= len(data) and data[pos : pos + 4] == _CD_ENTRY_SIG:
        crc, _compressed, size, name_len, extra_len, comment_len = struct.unpack(
            "<IIIHHH", data[pos + 16 : pos + 34]
        )
        name = data[pos + 46 : pos + 46 + name_len].decode("utf-8", "replace")
        entries[name] = (crc, size)
        pos += 46 + name_len + extra_len + comment_len
    return entries


def _locate_central_directory(tail: bytes, total_size: int) -> tuple[int, int]:
    """Return (offset, size) of the central directory, reading the end-of-archive records."""
    eocd = tail.rfind(_EOCD_SIG)
    if eocd < 0:
        raise PinError("Wheel is not a readable zip archive")
    cd_size, cd_offset = struct.unpack("<II", tail[eocd + 12 : eocd + 20])

    # Zip64 archives park the real values in a separate record and leave 0xFFFF.. here.
    if cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF:
        zip64 = tail.rfind(_ZIP64_EOCD_SIG)
        if zip64 < 0:
            raise PinError("Wheel needs a zip64 directory that is not present")
        cd_size, cd_offset = struct.unpack("<QQ", tail[zip64 + 40 : zip64 + 56])

    if cd_offset + cd_size > total_size:
        raise PinError("Wheel central directory is out of bounds")
    return cd_offset, cd_size


async def _fetch_range(session: aiohttp.ClientSession, url: str, start: int, end: int) -> bytes:
    """Return bytes [start, end] of a URL, tolerating a server that ignores Range."""
    async with session.get(
        url, headers={"Range": f"bytes={start}-{end}"}, timeout=aiohttp.ClientTimeout(total=120)
    ) as resp:
        status = resp.status
        if status not in (200, 206):
            raise PinError(f"Range request failed with HTTP {status}")
        data = await resp.read()
    if status == 200 and len(data) > end - start + 1:
        return data[start : end + 1]
    return data


async def async_component_entries(
    session: aiohttp.ClientSession, wheel: WheelInfo, domain: str
) -> dict[str, tuple[int, int]]:
    """Digest of homeassistant/components/<domain>/ inside a wheel, without downloading it."""
    tail_start = max(0, wheel.size - _TAIL_BYTES)
    tail = await _fetch_range(session, wheel.url, tail_start, wheel.size - 1)
    offset, size = _locate_central_directory(tail, wheel.size)

    if offset >= tail_start:
        directory = tail[offset - tail_start : offset - tail_start + size]
    else:
        directory = await _fetch_range(session, wheel.url, offset, offset + size - 1)

    prefix = f"homeassistant/components/{domain}/"
    return {
        name[len(prefix) :]: value
        for name, value in parse_central_directory(directory).items()
        if name.startswith(prefix) and not name.endswith("/") and "__pycache__/" not in name
    }


def local_component_digest(path: Path, ignore: set[str] | None = None) -> dict[str, tuple[int, int]]:
    """Same digest shape, computed over an integration directory on disk."""
    ignore = ignore or set()
    digest: dict[str, tuple[int, int]] = {}
    for file in path.rglob("*"):
        if not file.is_file() or "__pycache__" in file.parts:
            continue
        rel = file.relative_to(path).as_posix()
        if rel in ignore:
            continue
        try:
            data = file.read_bytes()
        except OSError:
            continue
        digest[rel] = (zlib.crc32(data), len(data))
    return digest


def compare_digests(
    base: dict[str, tuple[int, int]], candidate: dict[str, tuple[int, int]]
) -> dict[str, Any]:
    """What the candidate changes relative to the base."""
    added = sorted(set(candidate) - set(base))
    removed = sorted(set(base) - set(candidate))
    shared = set(base) & set(candidate)
    changed = sorted(name for name in shared if base[name] != candidate[name])
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged": len(shared) - len(changed),
    }


# ---------------------------------------------------------------------------
# Filesystem (blocking; run in executor)
# ---------------------------------------------------------------------------


def custom_components_dir(config_dir: str) -> Path:
    return Path(config_dir) / "custom_components"


def retired_dir(config_dir: str) -> Path:
    return Path(config_dir) / RETIRED_DIRNAME


def core_components_dir() -> Path:
    import homeassistant.components as components

    return Path(components.__path__[0])


def list_core_domains() -> list[str]:
    root = core_components_dir()
    return sorted(
        p.name for p in root.iterdir() if p.is_dir() and (p / "manifest.json").is_file()
    )


def core_dependents() -> dict[str, list[str]]:
    """Map each core domain to the core integrations that depend on it, transitively.

    Manifest edges only go one level deep, but risk does not: nothing declares
    `bluetooth` except a handful of integrations, while dozens reach it through
    `bluetooth_adapters`. Following the graph is what makes the count mean something.
    """
    root = core_components_dir()
    deps: dict[str, set[str]] = {}
    for path in root.iterdir():
        if not path.is_dir():
            continue
        manifest = read_manifest(path)
        if manifest is None:
            continue
        deps[path.name] = set(manifest.get("dependencies", [])) | set(
            manifest.get("after_dependencies", [])
        )

    dependents: dict[str, set[str]] = {}
    for domain, direct in deps.items():
        reached: set[str] = set()
        stack = list(direct)
        while stack:
            dep = stack.pop()
            if dep in reached:
                continue
            reached.add(dep)
            stack.extend(deps.get(dep, ()))
        reached.discard(domain)  # dependency cycles must not make a domain depend on itself
        for dep in reached:
            dependents.setdefault(dep, set()).add(domain)
    return {domain: sorted(names) for domain, names in dependents.items()}


def read_manifest(path: Path) -> dict[str, Any] | None:
    manifest = path / "manifest.json"
    if not manifest.is_file():
        return None
    try:
        return json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_marker(path: Path) -> dict[str, Any] | None:
    marker = path / MARKER_FILE
    if not marker.is_file():
        return None
    try:
        return json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def core_requirements(domain: str) -> list[str]:
    manifest = read_manifest(core_components_dir() / domain)
    return list(manifest.get("requirements", [])) if manifest else []


def extract_component(
    wheel_path: Path, domain: str, version: str, config_dir: str, core_version: str
) -> Path:
    """Extract homeassistant/components/<domain>/ from a wheel into custom_components.

    Writes to a temporary sibling directory first and swaps it in atomically-ish so a
    failed extraction never leaves a half-written override behind.
    """
    prefix = f"homeassistant/components/{domain}/"
    target = custom_components_dir(config_dir) / domain
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{domain}.", dir=target.parent))

    try:
        with zipfile.ZipFile(wheel_path) as zf:
            members = [m for m in zf.namelist() if m.startswith(prefix) and not m.endswith("/")]
            if not members:
                raise PinError(f"Home Assistant {version} has no core integration '{domain}'")
            for member in members:
                rel = member[len(prefix) :]
                if rel.startswith("__pycache__/") or "/__pycache__/" in rel:
                    continue
                out = staging / rel
                if not out.resolve().is_relative_to(staging.resolve()):
                    raise PinError("Refusing to extract path outside target directory")
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, out.open("wb") as dst:
                    shutil.copyfileobj(src, dst)

        manifest_path = staging / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        # Custom integrations must declare a version; core manifests don't.
        manifest["version"] = version
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

        write_marker(staging, domain, version, core_version)

        if target.exists():
            _retire(target, config_dir, f"{domain}-replaced")
        os.replace(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target


def write_marker(path: Path, domain: str, version: str, core_version: str) -> None:
    from homeassistant.util import dt as dt_util

    (path / MARKER_FILE).write_text(
        json.dumps(
            {
                "domain": domain,
                "pinned_version": version,
                "core_at_pin": core_version,
                "created": dt_util.utcnow().isoformat(),
                "managed_by": "integration_pins",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _retire(path: Path, config_dir: str, label: str) -> Path:
    from homeassistant.util import dt as dt_util

    dest_root = retired_dir(config_dir)
    dest_root.mkdir(parents=True, exist_ok=True)
    stamp = dt_util.utcnow().strftime("%Y%m%dT%H%M%SZ")
    dest = dest_root / f"{label}-{stamp}"
    shutil.move(str(path), str(dest))
    return dest


def remove_override(
    domain: str, pinned_version: str, config_dir: str, retire: bool = True
) -> Path | None:
    """Take an override out of custom_components. Returns the retired path, if kept."""
    target = custom_components_dir(config_dir) / domain
    if not target.exists():
        return None
    if not retire:
        shutil.rmtree(target)
        return None
    return _retire(target, config_dir, f"{domain}-{pinned_version}")


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def list_retired(config_dir: str) -> list[dict[str, Any]]:
    root = retired_dir(config_dir)
    if not root.is_dir():
        return []
    entries = []
    for path in root.iterdir():
        if not path.is_dir():
            continue
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        entries.append({"name": path.name, "bytes": _dir_size(path), "modified": modified})
    entries.sort(key=lambda e: e["modified"], reverse=True)
    return entries


def _retired_path(config_dir: str, name: str) -> Path:
    """Resolve a retired entry by name, refusing anything that leaves the folder."""
    root = retired_dir(config_dir).resolve()
    candidate = (root / name).resolve()
    if candidate.parent != root or not candidate.is_dir():
        raise PinError(f"'{name}' is not a retired override")
    return candidate


def delete_retired(config_dir: str, name: str) -> None:
    shutil.rmtree(_retired_path(config_dir, name))


def clear_retired(config_dir: str) -> int:
    root = retired_dir(config_dir)
    if not root.is_dir():
        return 0
    removed = 0
    for path in list(root.iterdir()):
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        removed += 1
    return removed


def inspect_override(domain: str, config_dir: str) -> dict[str, Any]:
    """Describe what is currently sitting in custom_components/<domain>."""
    path = custom_components_dir(config_dir) / domain
    if not path.is_dir():
        return {"present": False}
    manifest = read_manifest(path) or {}
    return {
        "present": True,
        "marker": read_marker(path),
        "manifest_version": manifest.get("version"),
        "requirements": list(manifest.get("requirements", [])),
        "core_requirements": core_requirements(domain),
    }


def _hacs_sources(config_dir: str) -> dict[str, str]:
    """Map domain -> HACS repository, read from HACS's own storage.

    Best effort: this is HACS's private file and its shape has changed between major
    versions, so walk it for anything that looks like a repository record instead of
    assuming a layout, and return nothing at all if it cannot be read.
    """
    path = Path(config_dir) / ".storage" / "hacs.data"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

    sources: dict[str, str] = {}

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        domain = node.get("domain")
        repo = node.get("full_name") or node.get("repository")
        if isinstance(domain, str) and isinstance(repo, str):
            sources.setdefault(domain, repo)
        for value in node.values():
            walk(value)

    walk(data)
    return sources


def list_custom_integrations(config_dir: str, exclude: set[str]) -> list[dict[str, Any]]:
    """Third-party integrations in custom_components that do not shadow a core domain."""
    ccdir = custom_components_dir(config_dir)
    if not ccdir.is_dir():
        return []
    core = set(list_core_domains())
    hacs = _hacs_sources(config_dir)
    result: list[dict[str, Any]] = []
    for path in sorted(ccdir.iterdir()):
        if not path.is_dir() or path.name in exclude or path.name in core:
            continue
        manifest = read_manifest(path)
        if manifest is None:
            continue
        repo = hacs.get(path.name)
        result.append(
            {
                "domain": path.name,
                "version": manifest.get("version"),
                "source": "hacs" if repo else "manual",
                "source_detail": repo or "",
            }
        )
    return result


def list_unmanaged_overrides(config_dir: str, managed: set[str]) -> list[dict[str, Any]]:
    """Custom components that shadow a core domain but are not pinned by us."""
    ccdir = custom_components_dir(config_dir)
    if not ccdir.is_dir():
        return []
    core = set(list_core_domains())
    result: list[dict[str, Any]] = []
    for path in sorted(ccdir.iterdir()):
        if not path.is_dir() or path.name in managed or path.name not in core:
            continue
        manifest = read_manifest(path)
        if manifest is None:
            continue
        result.append(
            {
                "domain": path.name,
                "manifest_version": manifest.get("version"),
                "marker": read_marker(path),
            }
        )
    return result
