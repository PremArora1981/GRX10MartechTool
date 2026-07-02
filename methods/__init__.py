"""GRX10 estimation-method framework.

Pluggable sizing methods (v1-definition §4) that turn normalised ``raw_*`` data
into ``cell_triangulation`` estimates. Public surface:

* :class:`methods.base.Method` — the ABC every method implements.
* :mod:`methods.registry` — ``method_code`` -> class registry, with the catalog
  loaded from ``config/methods.yaml`` and a ``get_method`` resolver.
"""

from methods.base import Method  # noqa: F401

__all__ = ["Method"]
