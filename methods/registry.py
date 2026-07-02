"""Method registry — maps ``method_code`` to Method classes; catalog from YAML.

The set of *valid* method codes and their metadata (tier, source_class,
``is_primary_source``, ``confidence_cap``, ``required_raw_tables``) is owned by
``config/methods.yaml`` and seeded into ``method_registry`` by the config loader.
This module loads that same YAML as the authoritative **catalog**, then maps each
code to the concrete :class:`~methods.base.Method` implementation that produces
its estimates.

Method modules register themselves declaratively::

    from methods.registry import register

    @register("comtrade_hs4_import")
    class ComtradeHs4Import(Method):
        ...

Resolution helpers used by ``pipeline/run.py``:

* :func:`get` / :func:`get_method` -> the Method **class** for a code (or ``None``).
* :func:`catalog` -> the YAML metadata for every declared method.

A code present in the catalog but lacking an implementation is reported by
:func:`unimplemented` (so it is visibly *missing*, never silently fabricated).
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from pathlib import Path
from typing import Any

import yaml

from methods.base import Method

logger = logging.getLogger("grx10.methods.registry")

# method_code -> Method subclass
REGISTRY: dict[str, type[Method]] = {}

# Repo root = parent of the methods/ package.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_METHODS_YAML = _REPO_ROOT / "config" / "methods.yaml"
_PACKAGE = "methods"

_DISCOVERED = False
_CATALOG: dict[str, dict[str, Any]] | None = None


# --------------------------------------------------------------------------- #
# Catalog (from config/methods.yaml)
# --------------------------------------------------------------------------- #
def catalog(force: bool = False) -> dict[str, dict[str, Any]]:
    """Return ``{method_code: metadata}`` loaded from ``config/methods.yaml``."""
    global _CATALOG
    if _CATALOG is not None and not force:
        return _CATALOG
    data: dict[str, Any] = {}
    if _METHODS_YAML.exists():
        try:
            with _METHODS_YAML.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read %s: %s", _METHODS_YAML, exc)
    else:
        logger.warning("methods.yaml not found at %s", _METHODS_YAML)
    _CATALOG = {
        str(m["method_code"]): m
        for m in data.get("methods", [])
        if m.get("method_code")
    }
    return _CATALOG


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
def register(code: str | None = None) -> Any:
    """Class decorator registering a :class:`Method` under ``code``.

    ``code`` defaults to the class's ``method_code`` attribute. The method's
    ``required_raw_tables`` is backfilled from the YAML catalog when the class
    leaves it empty, keeping a single source of truth for that metadata.
    """

    def _wrap(cls: type[Method]) -> type[Method]:
        if not (isinstance(cls, type) and issubclass(cls, Method)):
            raise TypeError(f"@register expects a Method subclass, got {cls!r}")
        name = code or getattr(cls, "method_code", "")
        if not name:
            raise ValueError(f"{cls.__name__} has no method_code to register under")
        cls.method_code = name
        meta = catalog().get(name)
        if meta and not cls.required_raw_tables:
            cls.required_raw_tables = list(meta.get("required_raw_tables", []) or [])
        if name not in catalog():
            logger.warning("method %r is not declared in methods.yaml", name)
        if name in REGISTRY and REGISTRY[name] is not cls:
            logger.warning("method code %r re-registered (%s -> %s)",
                           name, REGISTRY[name].__name__, cls.__name__)
        REGISTRY[name] = cls
        return cls

    return _wrap


def register_class(code: str, cls: type[Method]) -> None:
    """Imperative registration for dynamically constructed methods."""
    register(code)(cls)


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def discover(force: bool = False) -> None:
    """Import every method module so its ``@register`` calls run (once)."""
    global _DISCOVERED
    if _DISCOVERED and not force:
        return
    _DISCOVERED = True
    pkg_dir = Path(__file__).resolve().parent
    for finder, mod_name, is_pkg in pkgutil.iter_modules([str(pkg_dir)]):
        if mod_name in ("registry", "base", "__init__"):
            continue
        try:
            importlib.import_module(f"{_PACKAGE}.{mod_name}")
        except Exception as exc:  # noqa: BLE001 — isolate a bad method module
            logger.warning("skipping method module %s: %s", mod_name, exc)
    _harvest_subclasses(Method)


def _harvest_subclasses(base: type[Method]) -> None:
    for sub in base.__subclasses__():
        name = getattr(sub, "method_code", "")
        if name and name not in REGISTRY:
            REGISTRY[name] = sub
        _harvest_subclasses(sub)


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def get(code: str | None) -> type[Method] | None:
    """Return the Method class registered under ``code`` (or ``None``)."""
    if not code:
        return None
    discover()
    return REGISTRY.get(code)


#: Alias matching the name ``pipeline/run.py`` probes for first.
get_method = get


def create(code: str) -> Method | None:
    """Instantiate the Method registered under ``code`` (or ``None``)."""
    cls = get(code)
    return cls() if cls is not None else None


def available() -> list[str]:
    """Sorted list of implemented method codes (after discovery)."""
    discover()
    return sorted(REGISTRY)


def unimplemented() -> list[str]:
    """Catalog codes declared in YAML that have no registered implementation yet."""
    discover()
    return sorted(set(catalog()) - set(REGISTRY))
