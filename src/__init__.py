"""Compatibility namespace for legacy ``src.*`` producers and imports.

The implementation lives under :mod:`comsol_mcp`.  This package installs a
module alias finder so legacy imports resolve the exact canonical module
objects instead of creating a second singleton graph.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from importlib import import_module
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec
from importlib.util import find_spec
from types import CodeType, ModuleType

from comsol_mcp import __version__

_CANONICAL_PREFIX = "comsol_mcp"
_LEGACY_PREFIX = __name__
_FINDER_IDENTITY = "comsol_mcp.src_alias_finder.v1"


class _CanonicalAliasLoader(Loader):
    def __init__(self, legacy_name: str, canonical_name: str) -> None:
        self.legacy_name = legacy_name
        self.canonical_name = canonical_name
        self._canonical_metadata: dict[str, tuple[bool, object | None]] = {}

    def create_module(self, spec: ModuleSpec) -> ModuleType:
        module = import_module(self.canonical_name)
        self._canonical_metadata = {
            name: (hasattr(module, name), getattr(module, name, None))
            for name in (
                "__cached__",
                "__file__",
                "__loader__",
                "__name__",
                "__package__",
                "__spec__",
            )
        }
        sys.modules[self.legacy_name] = module
        return module

    def exec_module(self, module: ModuleType) -> None:
        # Importlib may initialize the returned canonical object with the
        # legacy alias spec. Restore the exact original metadata objects.
        for name, (present, value) in self._canonical_metadata.items():
            if not present:
                module.__dict__.pop(name, None)
            else:
                setattr(module, name, value)

    def get_code(self, fullname: str) -> CodeType | None:
        """Let legacy ``python -m`` commands execute the canonical module."""
        if fullname != self.legacy_name:
            return None
        source = (
            "from runpy import run_module as _run_module\n"
            f"_run_module({self.canonical_name!r}, run_name='__main__', alter_sys=True)\n"
        )
        return compile(source, f"<{self.legacy_name}-compatibility>", "exec")


class _CanonicalAliasFinder(MetaPathFinder):
    alias_finder_identity = _FINDER_IDENTITY

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        del path, target
        if not fullname.startswith(_LEGACY_PREFIX + "."):
            return None
        canonical_name = _CANONICAL_PREFIX + fullname[len(_LEGACY_PREFIX) :]
        canonical_spec = find_spec(canonical_name)
        if canonical_spec is None:
            return None
        is_package = canonical_spec.submodule_search_locations is not None
        return ModuleSpec(
            fullname,
            _CanonicalAliasLoader(fullname, canonical_name),
            is_package=is_package,
        )


if not any(
    getattr(item, "alias_finder_identity", None) == _FINDER_IDENTITY
    or (type(item).__module__ == __name__ and type(item).__name__ == "_CanonicalAliasFinder")
    for item in sys.meta_path
):
    sys.meta_path.insert(0, _CanonicalAliasFinder())

# Keep ``src`` importable as a package while preventing the import machinery
# from searching for duplicate implementation files below this compatibility
# directory.
__path__ = []

__all__ = ["__version__"]
