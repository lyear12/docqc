"""Checker plugin protocol and registry.

A checker is anything that can look at a :class:`~docqc.document.Document` and
return a :class:`~docqc.models.CheckResult`. Registration is by decorator, so
adding a check means adding one file and one import — nothing else in the
codebase changes.

Why a registry instead of a hard-coded list:

* The CLI, the web app, and the tests all need "every checker", and they must
  agree. One registry is the single source of that list.
* Checkers are independent by construction: they share an input and an output
  type and nothing else. That is what makes them individually testable.
* Ordering is explicit (``order``) rather than incidental, so report output is
  stable across runs — which matters when a report is diffed in CI.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .models import CheckResult

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, typing only
    from .document import Document


@runtime_checkable
class Checker(Protocol):
    """What every checker must provide.

    Implementations are plain classes; they receive the document in
    :meth:`run` rather than at construction, so one instance can be reused
    across documents and stays trivially testable.
    """

    name: str
    """Unique short identifier, ``lower_snake``. Becomes ``Finding.checker``."""

    description: str
    """One line, shown by ``docqc list`` and in the web UI."""

    def run(self, document: Document) -> CheckResult:
        """Check ``document`` and return a verdict.

        Implementations must not raise for expected problems — a malformed
        document is a finding, not an exception. Letting an unexpected exception
        escape is acceptable: the runner converts it into
        ``CheckResult(status=ERROR)`` so one broken checker cannot take down the
        whole run.

        Args:
            document: The parsed input.

        Returns:
            The checker's verdict.
        """
        ...


@dataclass(frozen=True, slots=True)
class CheckerEntry:
    """A registered checker plus the metadata the registry needs to order it."""

    name: str
    description: str
    factory: Callable[[], Checker]
    order: int = 100
    """Lower runs first. Leave gaps so checkers can be slotted in later."""


_REGISTRY: dict[str, CheckerEntry] = {}


def register(
    name: str,
    description: str,
    *,
    order: int = 100,
) -> Callable[[Callable[[], Checker]], Callable[[], Checker]]:
    """Decorator registering a checker factory under ``name``.

    Args:
        name: Unique ``lower_snake`` identifier.
        description: One-line summary for ``docqc list``.
        order: Execution order; lower first.

    Returns:
        The decorator, which returns the factory unchanged.

    Raises:
        ValueError: If ``name`` is already registered. Duplicates are a
            programming error, and silently overwriting one would make which
            checker ran depend on import order.
    """

    def decorate(factory: Callable[[], Checker]) -> Callable[[], Checker]:
        if name in _REGISTRY:
            raise ValueError(
                f"checker {name!r} is already registered by "
                f"{_REGISTRY[name].factory.__module__}; names must be unique"
            )
        _REGISTRY[name] = CheckerEntry(
            name=name, description=description, factory=factory, order=order
        )
        return factory

    return decorate


def registered() -> list[CheckerEntry]:
    """Every registered checker, in execution order.

    Returns:
        Entries sorted by ``(order, name)`` — the name tiebreak keeps the order
        deterministic when two checkers share an ``order``.
    """
    return sorted(_REGISTRY.values(), key=lambda e: (e.order, e.name))


def build(names: Sequence[str] | None = None) -> list[Checker]:
    """Instantiate checkers for a run.

    Args:
        names: When given, only these checkers, in this order. When ``None``,
            every registered checker in registry order.

    Returns:
        Ready-to-run checker instances.

    Raises:
        KeyError: If a requested name is not registered. Failing loudly beats
            silently running fewer checks than the user asked for.
    """
    if names is None:
        return [entry.factory() for entry in registered()]
    missing = [n for n in names if n not in _REGISTRY]
    if missing:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise KeyError(f"unknown checker(s): {', '.join(missing)}; known: {known}")
    return [_REGISTRY[n].factory() for n in names]


def clear() -> None:
    """Drop every registration.

    For tests only: the registry is process-global, so a test that registers a
    fixture checker must reset afterwards to avoid leaking into other tests.
    """
    _REGISTRY.clear()


__all__ = [
    "Checker",
    "CheckerEntry",
    "build",
    "clear",
    "register",
    "registered",
]
