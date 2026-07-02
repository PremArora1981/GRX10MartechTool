"""Connector registry — maps ``sources.connector`` names to Connector classes.

Connectors are plug-ins (v1-definition Q12): each module under ``connectors/``
(and ``connectors/families/``) registers its class(es) here, and the pipeline
resolves a source's ``connector`` string to a class via :func:`get` /
:func:`get_connector`.

Registration is either declarative::

    from connectors.registry import register

    @register("comtrade")
    class ComtradeConnector(Connector):
        ...

or implicit: any ``Connector`` subclass that sets a class attribute
``connector_name`` (or whose module is named after the connector) is picked up by
:func:`discover`, which imports every sibling module once on first use.

The registry is resilient: a connector module that fails to import (e.g. a
missing optional dependency) is logged and skipped, never crashing resolution of
the others.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from pathlib import Path
from typing import Any, Mapping

from connectors.base import Connector

logger = logging.getLogger("grx10.connectors.registry")

# name (sources.connector) -> Connector subclass
REGISTRY: dict[str, type[Connector]] = {}

_DISCOVERED = False
_PACKAGE = "connectors"


def register(name: str) -> Any:
    """Class decorator registering a :class:`Connector` under ``name``."""

    def _wrap(cls: type[Connector]) -> type[Connector]:
        if not (isinstance(cls, type) and issubclass(cls, Connector)):
            raise TypeError(f"@register expects a Connector subclass, got {cls!r}")
        if name in REGISTRY and REGISTRY[name] is not cls:
            logger.warning("connector name %r re-registered (%s -> %s)",
                           name, REGISTRY[name].__name__, cls.__name__)
        REGISTRY[name] = cls
        cls.connector_name = name  # type: ignore[attr-defined]
        return cls

    return _wrap


def register_class(name: str, cls: type[Connector]) -> None:
    """Imperative registration (for dynamically built family connectors)."""
    register(name)(cls)


def discover(force: bool = False) -> None:
    """Import every connector module so its ``@register`` calls run (once)."""
    global _DISCOVERED
    if _DISCOVERED and not force:
        return
    _DISCOVERED = True

    pkg_dir = Path(__file__).resolve().parent
    # Walk the connectors package + the families subpackage.
    for finder, mod_name, is_pkg in pkgutil.iter_modules([str(pkg_dir)]):
        if mod_name in ("registry", "base", "http", "__init__"):
            continue
        _safe_import(f"{_PACKAGE}.{mod_name}")
    families_dir = pkg_dir / "families"
    if families_dir.is_dir():
        for finder, mod_name, is_pkg in pkgutil.iter_modules([str(families_dir)]):
            if mod_name in ("rest_family", "scrape_base", "__init__"):
                continue  # base/family modules expose no concrete sources
            _safe_import(f"{_PACKAGE}.families.{mod_name}")

    # Implicit pickup: any imported Connector subclass advertising a name.
    _harvest_subclasses(Connector)


def _safe_import(module: str) -> None:
    try:
        importlib.import_module(module)
    except Exception as exc:  # noqa: BLE001 — one bad module must not break the rest
        logger.warning("skipping connector module %s: %s", module, exc)


def _harvest_subclasses(base: type[Connector]) -> None:
    for sub in base.__subclasses__():
        name = getattr(sub, "connector_name", None)
        if name and name not in REGISTRY:
            REGISTRY[name] = sub
        _harvest_subclasses(sub)


def get(name: str | None) -> type[Connector] | None:
    """Return the Connector class registered under ``name`` (or ``None``)."""
    if not name:
        return None
    discover()
    return REGISTRY.get(name)


def get_connector(
    source_row: Mapping[str, Any] | str,
    credential: str | None = None,
) -> Any:
    """Factory: build a Connector instance for a seeded source row.

    Two call shapes are supported:

    * ``get_connector(source_row_dict, credential)`` — the documented factory;
      returns an **instance** bound to the row + decrypted secret, or ``None`` if
      the row names no known connector.
    * ``get_connector("connector_name")`` — name-only lookup; returns the
      **class** (used by ``pipeline/run.py``'s generic resolver, which then
      instantiates it itself).
    """
    if isinstance(source_row, str):
        return get(source_row)
    name = source_row.get("connector") or source_row.get("source_id")
    cls = get(name)
    if cls is None:
        logger.warning("no connector registered for %r", name)
        return None
    return cls(source_row, credential)


def available() -> list[str]:
    """Sorted list of registered connector names (after discovery)."""
    discover()
    return sorted(REGISTRY)
