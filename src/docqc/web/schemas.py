"""Request and response models for the HTTP API.

These are a *transport* contract, deliberately separate from
:mod:`docqc.models`. The domain models describe what a check found; these
describe what a client sends and receives. Keeping them apart means the API can
evolve (add pagination, rename a field, split a payload) without perturbing the
checkers or the report format the CLI writes.

Every response is JSON-serializable and carries no Python objects — the same
rule the domain models follow, for the same reason.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CheckerInfo(BaseModel):
    """One registered checker, as listed by ``GET /api/checkers``."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    order: int


class BackendInfo(BaseModel):
    """One rendering backend, as listed by ``GET /api/backends``."""

    model_config = ConfigDict(extra="forbid")

    name: str
    available: bool
    detail: str


class JobCreated(BaseModel):
    """Returned by ``POST /api/jobs`` once a document has been accepted.

    The client subscribes to ``/api/jobs/{job_id}/events`` for progress. It does
    not need the id to fetch the result lazily — the stream carries the report —
    but the id makes the job resumable if the browser reloads mid-run.
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str
    filename: str
    size_bytes: int
    checkers: list[str] = Field(default_factory=list)
    events_url: str


class ProgressEvent(BaseModel):
    """A status update pushed over SSE while checkers run.

    Attributes:
        kind: One of ``started``, ``checker_started``, ``checker_finished``,
            ``finished``, ``failed``.
        message: Human-readable line for the activity log.
        checker: Which checker this event concerns, when applicable.
        index: 1-based position of ``checker`` in the run.
        total: How many checkers this run will execute.
        report: The full report, present only on ``finished``.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str
    message: str
    checker: str | None = None
    index: int | None = None
    total: int | None = None
    report: dict[str, Any] | None = None


class JobStatus(BaseModel):
    """Polling view of a job, for clients that cannot hold an SSE connection."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    state: str
    """``pending``, ``running``, ``done``, or ``failed``."""
    filename: str
    completed: int = 0
    total: int = 0
    error: str | None = None
    report: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    """A failed request, with a message meant to be shown to a person."""

    model_config = ConfigDict(extra="forbid")

    error: str
    detail: str | None = None


__all__ = [
    "BackendInfo",
    "CheckerInfo",
    "ErrorResponse",
    "JobCreated",
    "JobStatus",
    "ProgressEvent",
]
