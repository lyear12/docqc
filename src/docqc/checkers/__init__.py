"""Checker plugins.

Every checker is registered by importing its module, so this package's
``__init__`` is the one place that decides what ``docqc`` can check. Import
order does not matter for correctness — :func:`docqc.registry.registered` sorts
by ``(order, name)`` — but keeping it alphabetical makes the list easy to scan.
"""

from __future__ import annotations

from . import formula, layout, structure

__all__ = ["formula", "layout", "structure"]
