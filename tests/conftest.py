"""Test fixtures."""

from __future__ import annotations

import asyncio
import builtins
import functools
import importlib.util
import inspect
import io
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


_PACKAGE_DIR = str(Path(__file__).resolve().parent.parent / "custom_components" / "integration_pins")
_COVERAGE = importlib.util.find_spec("coverage")
_COVERAGE_DIR = str(Path(_COVERAGE.origin).parent) if _COVERAGE else "\0"

# Home Assistant's own loop protection skips file calls under pytest, so these stand in
# for it. Path, tempfile, zipfile and shutil all come down to one of them.
_BLOCKING_FILE_CALLS = (
    (builtins, "open"),
    (io, "open"),
    (os, "open"),
    (os, "stat"),
    (os, "lstat"),
    (os, "listdir"),
    (os, "scandir"),
    (os, "mkdir"),
    (os, "rmdir"),
    (os, "unlink"),
    (os, "remove"),
    (os, "rename"),
    (os, "replace"),
    (shutil, "rmtree"),
    (shutil, "move"),
)


def _integration_coroutine_on_the_loop() -> str | None:
    """Where integration code running on the event loop made this call, if it did.

    An executor thread has no running loop. A test calling a blocking helper directly is
    the test's own business, so only a call made beneath one of the integration's
    coroutines or `async_` callbacks counts.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return None
    frame = sys._getframe(2)
    while frame is not None:
        code = frame.f_code
        if code.co_filename.startswith(_COVERAGE_DIR):
            return None  # coverage's tracer resolving a file it has just seen
        if code.co_filename.startswith(_PACKAGE_DIR) and (
            code.co_flags & inspect.CO_COROUTINE or code.co_name.startswith(("async_", "_async_"))
        ):
            return f"{Path(code.co_filename).name}:{frame.f_lineno} in {code.co_name}"
        frame = frame.f_back
    return None


@pytest.fixture(autouse=True)
def no_blocking_file_io_on_the_loop():
    """Fail any test in which the integration touches the filesystem on the event loop."""
    violations: list[str] = []

    def guard(module, name, original):
        @functools.wraps(original)
        def guarded(*args, **kwargs):
            if (where := _integration_coroutine_on_the_loop()) is not None:
                violations.append(f"{module.__name__}.{name}({args[0] if args else ''!r}) at {where}")
            return original(*args, **kwargs)

        return guarded

    with pytest.MonkeyPatch.context() as mp:
        for module, name in _BLOCKING_FILE_CALLS:
            mp.setattr(module, name, guard(module, name, getattr(module, name)))
        yield violations
    assert not violations, "blocking file I/O on the event loop:\n" + "\n".join(violations)


FAKE_VERSION = "2025.12.3"
FAKE_DOMAIN = "sun"  # any real core domain works; we only need the name to exist


def build_fake_wheel(
    path: Path,
    version: str = FAKE_VERSION,
    domain: str = FAKE_DOMAIN,
    extra: dict[str, str] | None = None,
) -> Path:
    """Build a minimal wheel-shaped zip containing one integration."""
    prefix = f"homeassistant/components/{domain}/"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:  # as real wheels are
        zf.writestr(f"{prefix}__init__.py", f'"""fake {domain} from {version}"""\n')
        zf.writestr(f"{prefix}sensor.py", "# fake platform\n")
        zf.writestr(
            f"{prefix}manifest.json",
            json.dumps(
                {
                    "domain": domain,
                    "name": "Fake",
                    "codeowners": [],
                    "documentation": "https://example.invalid",
                    "requirements": ["fakelib==1.0.0"],
                }
            ),
        )
        zf.writestr(f"{prefix}translations/en.json", "{}")
        zf.writestr(f"{prefix}__pycache__/x.cpython-313.pyc", b"junk")
        zf.writestr("homeassistant/components/other/__init__.py", "")
        for name, text in (extra or {}).items():
            zf.writestr(f"{prefix}{name}", text)
    wheel = path / f"homeassistant-{version}-py3-none-any.whl"
    wheel.write_bytes(buf.getvalue())
    return wheel


@pytest.fixture
def fake_wheel(tmp_path: Path) -> Path:
    return build_fake_wheel(tmp_path)
