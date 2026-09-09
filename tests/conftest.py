"""Test fixtures."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


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
