"""Compatibility namespace for legacy ``src.*`` producers and imports.

The implementation lives under :mod:`comsol_mcp`.  This package installs a
module alias finder so legacy imports resolve the exact canonical module
objects instead of creating a second singleton graph.
"""

from __future__ import annotations

from importlib import import_module
from importlib.abc import Loader, MetaPathFinder
from importlib.util import find_spec
import sys
from types import ModuleType

from comsol_mcp import __version__


_CANONICAL_PREFIX = "comsol_mcp"
_LEGACY_PREFIX = __name__


class _CanonicalAliasLoader(Loader):
    def __init__(self, legacy_name: str, canonical_name: str) -> None:
        self.legacy_name = legacy_name
        self.canonical_name = canonical_name

    def create_module(self, spec):
        module = import_module(self.canonical_name)
        sys.modules[self.legacy_name] = module
        return module

    def exec_module(self, module: ModuleType) -> None:
        # Importlib initializes the returned canonical object with the legacy
        # alias spec. Restore canonical metadata so relative imports and
        # introspection never observe a split package identity.
        from importlib.machinery import ModuleSpec

        is_package = hasattr(module, "__path__")
        canonical_spec = ModuleSpec(
            self.canonical_name,
            getattr(module, "__loader__", None),
            is_package=is_package,
        )
        canonical_spec.origin = getattr(module, "__file__", None)
        if is_package:
            canonical_spec.submodule_search_locations = list(module.__path__)
        module.__spec__ = canonical_spec
        module.__package__ = (
            self.canonical_name
            if is_package
            else self.canonical_name.rpartition(".")[0]
        )

    def get_code(self, fullname: str):
        """Let legacy ``python -m`` commands execute the canonical module."""
        if fullname != self.legacy_name:
            return None
        source = (
            "from runpy import run_module as _run_module\n"
            f"_run_module({self.canonical_name!r}, run_name='__main__')\n"
        )
        return compile(source, f"<{self.legacy_name}-compatibility>", "exec")


class _CanonicalAliasFinder(MetaPathFinder):
    def find_spec(self, fullname: str, path=None, target=None):
        del path, target
        if not fullname.startswith(_LEGACY_PREFIX + "."):
            return None
        canonical_name = _CANONICAL_PREFIX + fullname[len(_LEGACY_PREFIX) :]
        canonical_spec = find_spec(canonical_name)
        if canonical_spec is None:
            return None
        is_package = canonical_spec.submodule_search_locations is not None
        from importlib.machinery import ModuleSpec

        return ModuleSpec(
            fullname,
            _CanonicalAliasLoader(fullname, canonical_name),
            is_package=is_package,
        )


if not any(isinstance(item, _CanonicalAliasFinder) for item in sys.meta_path):
    sys.meta_path.insert(0, _CanonicalAliasFinder())

# Keep ``src`` importable as a package while preventing the import machinery
# from searching for duplicate implementation files below this compatibility
# directory.
__path__ = []

__all__ = ["__version__"]
