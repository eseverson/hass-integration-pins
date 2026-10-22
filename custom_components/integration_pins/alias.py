"""Make a pin the code that integrations importing its domain get.

Home Assistant loads a pin as custom_components.<domain>, but an integration built on it
imports homeassistant.components.<domain>, which is the bundled package. Nothing joins the
two, so without help the importer runs the bundled code alongside the pin, with both
touching the same hass.data keys.

Every pin this integration writes carries SHIM_SOURCE as SHIM_FILE, imported by one line
at the top of the pin's __init__.py. The shim does not import Integration Pins, so a pin
keeps working after this integration is removed.
"""

from __future__ import annotations

import ast
import sys

SHIM_FILE = "_integration_pins_alias.py"
INIT_LINE = "from . import _integration_pins_alias  # noqa: F401  # added by Integration Pins\n"

# What the panel reports about who gets the pinned code.
IMPORTERS_PINNED = "pinned"  # homeassistant.components.<domain> is the pin
IMPORTERS_BUNDLED = "bundled"  # it is the bundled package, loaded beside the pin

SHIM_SOURCE = '''"""Written by Integration Pins: makes homeassistant.components.<domain> this pin.

Home Assistant loads the pin as custom_components.<domain>, while integrations built on it
import homeassistant.components.<domain>. This points that name, and every module under
it, at the pinned modules, so only one copy runs. A module the pinned code does not have
fails to import rather than coming from the bundled package.

It runs from the top of the pin's __init__.py. Home Assistant sets an integration up
before the ones that depend on it, so their imports come later. If the bundled package
was imported first, two copies are already running and aliasing the rest would mix them,
so it changes nothing and says so in the log.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import logging
import sys

PINNED = __package__
BUNDLED = "homeassistant.components." + PINNED.rpartition(".")[2]


class _Loader(importlib.abc.Loader):
    def __init__(self, target: str) -> None:
        self.target = target
        self.spec = None

    def create_module(self, spec):
        module = importlib.import_module(self.target)
        self.spec = module.__spec__
        return module

    def exec_module(self, module) -> None:
        # Already executed under its pinned name; importlib only replaced its __spec__.
        module.__spec__ = self.spec


class _Finder(importlib.abc.MetaPathFinder):
    pinned = PINNED

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(BUNDLED + "."):
            return None
        return importlib.util.spec_from_loader(
            fullname, _Loader(PINNED + fullname[len(BUNDLED) :])
        )


def _install() -> bool:
    pinned = sys.modules[PINNED]
    current = sys.modules.get(BUNDLED)
    if current is pinned:
        return True
    if current is not None:
        logging.getLogger(PINNED).warning(
            "%s was imported before this pin loaded, so integrations that import from it "
            "run the bundled code beside the pin",
            BUNDLED,
        )
        return False
    sys.meta_path.insert(0, _Finder())
    sys.modules[BUNDLED] = pinned
    setattr(sys.modules["homeassistant.components"], PINNED.rpartition(".")[2], pinned)
    return True


ALIASED = _install()
'''


def inject(init_source: str) -> str:
    """`init_source` with INIT_LINE after its docstring and __future__ imports."""
    lines = init_source.splitlines(keepends=True)
    after = 0
    try:
        body = ast.parse(init_source).body
    except (SyntaxError, ValueError):  # it will not import either way
        body = []
    for index, node in enumerate(body):
        docstring = (
            index == 0
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
        if not docstring and not (isinstance(node, ast.ImportFrom) and node.module == "__future__"):
            break
        after = node.end_lineno or after
    if after and not lines[after - 1].endswith("\n"):
        lines[after - 1] += "\n"
    return "".join([*lines[:after], INIT_LINE, *lines[after:]])


def strip(init_source: bytes) -> bytes:
    """`init_source` as it was before inject()."""
    return init_source.replace(INIT_LINE.encode(), b"", 1)


def importers(domain: str) -> str | None:
    """Which copy homeassistant.components.<domain> is in this process, once the pin runs.

    None while the pin is not loaded or nothing has imported the bundled name.
    """
    pinned = sys.modules.get(f"custom_components.{domain}")
    bundled = sys.modules.get(f"homeassistant.components.{domain}")
    if pinned is None or bundled is None:
        return None
    return IMPORTERS_PINNED if bundled is pinned else IMPORTERS_BUNDLED


def preimported_domains() -> set[str]:
    """Domains Home Assistant's bootstrap imports before any custom integration loads.

    A pin of one of these can never take over its bundled name. This is what bootstrap
    names itself; whatever those modules import in turn shows up after a restart instead.
    """
    from homeassistant import bootstrap  # noqa: PLC0415 -- already loaded in a running core

    prefix = "homeassistant.components."
    return {
        value.__name__[len(prefix) :].partition(".")[0]
        for value in vars(bootstrap).values()
        if isinstance(value, type(sys)) and value.__name__.startswith(prefix)
    }
