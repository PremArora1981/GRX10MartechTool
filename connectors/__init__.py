"""GRX10 connector framework.

Plug-in connectors (v1-definition Q12) that pull verbatim source payloads into
the ``raw_*`` layer. Public surface:

* :class:`connectors.base.Connector` — the ABC every connector implements.
* :class:`connectors.base.ProbeResult` / ``ProbeStatus`` — the 7-state health taxonomy.
* :func:`connectors.base.classify_http_error` — HTTP status -> taxonomy.
* :mod:`connectors.registry` — name -> class registry + ``get_connector`` factory.
* :mod:`connectors.families` — declarative REST + scrape base classes.
"""

from connectors.base import (  # noqa: F401
    Connector,
    ProbeResult,
    ProbeStatus,
    classify_exception,
    classify_http_error,
)

__all__ = [
    "Connector",
    "ProbeResult",
    "ProbeStatus",
    "classify_http_error",
    "classify_exception",
]
