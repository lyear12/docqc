"""Canonical data model for docqc.

Every checker speaks this vocabulary and nothing else, so the CLI and the future
web UI render one shape of result. Three levels, smallest to largest:

    Finding  -> one concrete problem, with the evidence needed to act on it
    CheckResult -> what one checker concluded (its findings plus its own status)
    Report  -> one whole run: the document, every checker, and a summary

Design rules worth keeping:

* **A finding must be actionable.** ``message`` says what is wrong; ``location``
  says where; ``evidence`` carries the raw values that justify the claim, so a
  reader can re-derive the verdict instead of trusting it.
* **Never invent a severity.** ``Severity`` has a fixed meaning (see below) and
  checkers pick from it; they do not get a numeric "score".
* **Failure is a first-class result.** A checker that crashes yields
  ``CheckResult(status=ERROR)`` rather than raising, so one broken checker never
  destroys the whole report.
* **Everything is JSON-serializable.** No datetimes, Paths, or sets leak into the
  payload: use ``str`` and ISO-8601 text so the report is portable.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0"


class Severity(StrEnum):
    """How much a finding matters.

    The three levels are deliberately about *consequence*, not about how hard the
    problem was to detect:

    ERROR   -- the deliverable is wrong or will not render as intended.
               Ship-blocking: a reviewer would see it as a defect.
    WARNING -- acceptable in isolation but a reviewer would question it.
               Fix before submitting if there is time.
    INFO    -- an observation worth knowing, not a defect. Counts, coverage
               notes, and "this looked fine" confirmations live here.
    """

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Status(StrEnum):
    """The outcome of running one checker."""

    PASS = "pass"
    """Ran to completion and found nothing at WARNING or above."""

    FAIL = "fail"
    """Ran to completion and found at least one ERROR."""

    WARN = "warn"
    """Ran to completion; no ERROR, but at least one WARNING."""

    SKIPPED = "skipped"
    """Deliberately not run (e.g. a checker whose input is absent)."""

    ERROR = "error"
    """The checker itself crashed. ``CheckResult.error`` explains why."""


class Location(BaseModel):
    """Where in the document a finding lives.

    All fields are optional because not every finding has every coordinate. Use
    whatever the format genuinely provides — do not fabricate a page number for a
    finding that came from the XML layer and never saw a rendered page.

    Attributes:
        page: 1-based page number, when known (rendered layer only).
        section: 1-based section index, when known.
        paragraph: 0-based index of the paragraph within the document body.
        table: 0-based index of the table within the document body.
        row: 0-based row index inside ``table``.
        column: 0-based column index inside ``table``.
        part: OOXML part name, e.g. ``word/document.xml``.
        text: The offending text itself, truncated to a readable length.
    """

    model_config = ConfigDict(extra="forbid")

    page: int | None = None
    section: int | None = None
    paragraph: int | None = None
    table: int | None = None
    row: int | None = None
    column: int | None = None
    part: str | None = None
    text: str | None = None

    def describe(self) -> str:
        """Render a short human-readable coordinate, e.g. ``p.3 ¶42``.

        Returns:
            A compact label; ``"(document)"`` when nothing is known.
        """
        bits: list[str] = []
        if self.page is not None:
            bits.append(f"p.{self.page}")
        if self.section is not None:
            bits.append(f"§{self.section}")
        if self.paragraph is not None:
            bits.append(f"¶{self.paragraph}")
        if self.table is not None:
            where = f"tbl{self.table}"
            if self.row is not None:
                where += f"[{self.row}"
                where += f",{self.column}]" if self.column is not None else "]"
            bits.append(where)
        return " ".join(bits) if bits else "(document)"


class Finding(BaseModel):
    """One concrete problem, with enough evidence to act on it.

    Attributes:
        checker: Name of the checker that produced this finding.
        code: Stable machine-readable identifier, ``UPPER_SNAKE``, unique within
            the checker. Downstream tooling keys off this, so never reuse a code
            for a different meaning.
        severity: See :class:`Severity`.
        message: One sentence stating what is wrong, in the document's own
            language (Chinese for Chinese deliverables).
        location: Where it lives.
        evidence: Raw values behind the claim — counts, thresholds, snippets.
            Keep it small and JSON-safe; this is what lets a reader verify the
            verdict instead of trusting it.
        hint: Optional suggested fix.
    """

    model_config = ConfigDict(extra="forbid")

    checker: str
    code: str
    severity: Severity
    message: str
    location: Location = Field(default_factory=Location)
    evidence: dict[str, Any] = Field(default_factory=dict)
    hint: str | None = None


class CheckResult(BaseModel):
    """What one checker concluded about one document."""

    model_config = ConfigDict(extra="forbid")

    checker: str
    status: Status
    findings: list[Finding] = Field(default_factory=list)
    duration_ms: int = 0
    error: str | None = None
    """Populated only when ``status`` is :attr:`Status.ERROR`."""

    @property
    def counts(self) -> dict[str, int]:
        """Number of findings per severity, always with all three keys present.

        Returns:
            Mapping of severity value to count.
        """
        tally = {s.value: 0 for s in Severity}
        for finding in self.findings:
            tally[finding.severity.value] += 1
        return tally


class Summary(BaseModel):
    """Run-level rollup, computed once from all check results."""

    model_config = ConfigDict(extra="forbid")

    status: Status
    errors: int = 0
    warnings: int = 0
    infos: int = 0
    checkers_run: int = 0
    checkers_errored: int = 0


class DocumentInfo(BaseModel):
    """What docqc learned about the input before checking it."""

    model_config = ConfigDict(extra="forbid")

    path: str
    name: str
    format: str
    """Lower-case extension without the dot, e.g. ``docx``."""
    size_bytes: int = 0
    sha256: str = ""
    """Content hash, so a report can be tied to an exact revision."""
    page_count: int | None = None
    paragraph_count: int | None = None
    table_count: int | None = None


class Report(BaseModel):
    """One whole run: the document, every checker's verdict, and a summary.

    This is the single artifact the CLI prints, the web UI renders, and tests
    assert against.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    tool_version: str = "0.1.0"
    generated_at: str
    """ISO-8601 UTC timestamp."""
    document: DocumentInfo
    results: list[CheckResult] = Field(default_factory=list)
    summary: Summary

    def findings(self, severity: Severity | None = None) -> list[Finding]:
        """Flatten findings across checkers, optionally filtered by severity.

        Args:
            severity: When given, keep only findings of this severity.

        Returns:
            Findings in checker order.
        """
        flat = [f for r in self.results for f in r.findings]
        if severity is None:
            return flat
        return [f for f in flat if f.severity is severity]


def summarize(results: list[CheckResult]) -> Summary:
    """Compute the run-level rollup from every checker's result.

    The overall status is the worst thing that happened: an errored checker
    outranks a failure, which outranks a warning, which outranks a pass.

    Args:
        results: One entry per checker that was attempted.

    Returns:
        The rollup, with per-severity totals already counted.
    """
    errors = warnings = infos = errored = 0
    for result in results:
        tally = result.counts
        errors += tally[Severity.ERROR.value]
        warnings += tally[Severity.WARNING.value]
        infos += tally[Severity.INFO.value]
        if result.status is Status.ERROR:
            errored += 1

    if errored:
        overall = Status.ERROR
    elif errors:
        overall = Status.FAIL
    elif warnings:
        overall = Status.WARN
    else:
        overall = Status.PASS

    return Summary(
        status=overall,
        errors=errors,
        warnings=warnings,
        infos=infos,
        checkers_run=len(results),
        checkers_errored=errored,
    )
