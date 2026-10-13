"""Run checkers against a document and assemble the report.

The runner owns three policies so no checker has to:

1. **Isolation.** A checker that raises becomes ``CheckResult(status=ERROR)``
   instead of aborting the run. One broken checker must never cost the user the
   results of the others.
2. **Timing.** Each checker is timed, because "which check is slow" is the first
   question when a run feels sluggish.
3. **Assembly.** Findings and the run-level summary are combined into one
   :class:`~docqc.models.Report` — the single artifact everything downstream
   consumes.
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

# Importing the checkers package is what populates the registry: each module
# registers itself by decorator at import time. Without this line the registry
# is empty and every run reports "unknown checker".
from . import checkers as _checkers  # noqa: F401  (imported for side effects)
from .document import Document
from .models import CheckResult, Finding, Report, Status, summarize
from .registry import Checker, build

ProgressFn = Callable[[str, int, int], None]
"""Called as ``progress(checker_name, index, total)`` before each checker runs."""


def run_checks(
    document: Document,
    *,
    names: Sequence[str] | None = None,
    progress: ProgressFn | None = None,
) -> list[CheckResult]:
    """Run every requested checker, isolating failures.

    Args:
        document: The parsed input, shared by all checkers.
        names: Restrict to these checker names, in this order. ``None`` runs all
            registered checkers.
        progress: Optional callback invoked before each checker starts; used by
            the CLI spinner and the web app's SSE stream.

    Returns:
        One result per checker, in execution order. A checker that raised yields
        ``status=ERROR`` with ``error`` set rather than being omitted.

    Raises:
        KeyError: If ``names`` includes an unregistered checker. Asking for a
            checker that does not exist is a caller bug, not a document problem.
    """
    checkers: list[Checker] = build(names)
    total = len(checkers)
    results: list[CheckResult] = []

    for index, checker in enumerate(checkers, start=1):
        if progress is not None:
            progress(checker.name, index, total)
        started = time.perf_counter()
        try:
            result = checker.run(document)
        except Exception as exc:  # noqa: BLE001 - isolation is the whole point
            elapsed = int((time.perf_counter() - started) * 1000)
            results.append(
                CheckResult(
                    checker=checker.name,
                    status=Status.ERROR,
                    findings=[],
                    duration_ms=elapsed,
                    error=f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}",
                )
            )
        else:
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            results.append(result)

    return results


def build_report(document: Document, results: list[CheckResult]) -> Report:
    """Assemble the run's report.

    Args:
        document: The document that was checked.
        results: Output of :func:`run_checks`.

    Returns:
        A complete :class:`~docqc.models.Report`.
    """
    from . import __version__

    return Report(
        tool_version=__version__,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        document=document.info(),
        results=results,
        summary=summarize(results),
    )


def check(
    path: Path | str,
    *,
    names: Sequence[str] | None = None,
    progress: ProgressFn | None = None,
) -> Report:
    """Load, check, and report in one call — the whole pipeline.

    Args:
        path: Document to inspect.
        names: Restrict to these checkers.
        progress: Optional per-checker callback.

    Returns:
        The finished report.
    """
    document = Document(path)
    results = run_checks(document, names=names, progress=progress)
    return build_report(document, results)


def top_findings(report: Report, limit: int = 10) -> list[Finding]:
    """The most important findings, for a terminal summary.

    Errors come first, then warnings, then info; within a severity the original
    checker order is preserved so the output is stable across runs.

    Args:
        report: A finished report.
        limit: Maximum findings to return.

    Returns:
        Up to ``limit`` findings, most severe first.
    """
    order = {"error": 0, "warning": 1, "info": 2}
    ranked = sorted(report.findings(), key=lambda f: order[f.severity.value])
    return ranked[:limit]
