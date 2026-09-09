"""Constants for Integration Pins."""

from __future__ import annotations

DOMAIN = "integration_pins"

STORAGE_KEY = DOMAIN
STORAGE_VERSION = 1

PANEL_URL_PATH = "integration-pins"
PANEL_TITLE = "Integration Pins"
PANEL_ICON = "mdi:pin"
PANEL_WEBCOMPONENT = "integration-pins-panel"
PANEL_STATIC_URL = "/integration_pins_static"
PANEL_JS_FILE = "integration-pins-panel.js"

# Marker file written into every override directory this integration manages.
MARKER_FILE = ".integration_pin.json"

# Where unpinned override directories are moved (relative to the config dir).
RETIRED_DIRNAME = "integration_pins_retired"

# Where a pin's code came from.
SOURCE_PYPI = "pypi"  # extracted from a release wheel
SOURCE_GIT = "git"  # fetched from a commit in the core repository
SOURCE_ADOPTED = "adopted"  # a directory that was already there

GITHUB_REPO = "home-assistant/core"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}"
GITHUB_RAW_URL = f"https://raw.githubusercontent.com/{GITHUB_REPO}"

PYPI_PROJECT = "homeassistant"
PYPI_JSON_URL = f"https://pypi.org/pypi/{PYPI_PROJECT}/json"
PYPI_VERSION_URL = f"https://pypi.org/pypi/{PYPI_PROJECT}/{{version}}/json"

# Pin status values (also used by the frontend).
STATUS_ACTIVE = "active"  # override present, core version inside range
STATUS_PENDING = "pending"  # override written, but Home Assistant still runs the old code
STATUS_OUT_OF_RANGE = "out_of_range"  # override present, core version outside range
STATUS_MISSING = "missing"  # pin recorded but override directory is gone
STATUS_FOREIGN = "foreign"  # directory exists but is not the one we wrote

ISSUE_OUT_OF_RANGE = "pin_out_of_range"
ISSUE_MISSING = "pin_missing"
ISSUE_FOREIGN = "pin_foreign"
