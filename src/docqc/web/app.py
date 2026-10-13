"""The FastAPI application.

Routes::

    GET  /api/health              liveness + version
    GET  /api/checkers            what can be checked
    GET  /api/backends            which renderers this machine has
    POST /api/jobs                upload a document, get a job id
    GET  /api/jobs/{id}           poll a job
    GET  /api/jobs/{id}/events    follow a job over SSE
    DELETE /api/jobs/{id}         drop a job and its upload

The app owns no checking logic. It validates transport concerns (file type,
size), hands the bytes to :class:`~docqc.web.jobs.JobStore`, and serializes what
comes back. That is what keeps a CLI run and a web run byte-identical in their
verdicts.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..models import Status
from ..registry import registered
from .jobs import STATE_DONE, STATE_FAILED, JobStore
from .schemas import BackendInfo, CheckerInfo, ErrorResponse, JobCreated, JobStatus

#: Largest upload accepted. A Word document with many embedded images can reach
#: tens of megabytes; 64 MiB leaves generous headroom without letting a mistake
#: fill the disk.
MAX_UPLOAD_BYTES = 64 * 1024 * 1024

#: Extensions the API accepts. Checked before the bytes are written, so an
#: unsupported upload costs nothing.
ALLOWED_SUFFIXES = frozenset({".docx"})

#: How long the SSE generator waits for the next event before emitting a
#: keep-alive comment. Proxies and browsers drop idle connections, and a layout
#: check can spend minutes inside Word with nothing to report.
STREAM_TIMEOUT_SECONDS = 20.0


def create_app(*, work_dir: Path | None = None, static_dir: Path | None = None) -> FastAPI:
    """Build the application.

    A factory rather than a module-level ``app`` so tests can point the store at
    a temporary directory and get a clean instance per test.

    Args:
        work_dir: Where uploads are written. Defaults to ``.docqc/uploads``
            under the current working directory.
        static_dir: Built frontend assets to serve at ``/``. Defaults to
            ``frontend/dist`` next to the package when it exists, so a source
            checkout works without configuration. When absent, only the API is
            served — development runs the UI through Vite instead.

    Returns:
        The configured application.
    """
    work_dir = work_dir or (Path.cwd() / ".docqc" / "uploads")
    store = JobStore(work_dir)

    app = FastAPI(
        title="docqc",
        version=__version__,
        summary="文档质检工具：排版、公式、结构",
        description=(
            "在交付前自动检查 Word 文档。上传一个 .docx，得到一个 JSON 报告。"
        ),
    )

    # The Vite dev server runs on another origin during development. Allowed
    # only for localhost: this is a local tool, and a permissive CORS policy
    # would let any page in the user's browser drive it.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    app.state.store = store

    # -- metadata ---------------------------------------------------------

    @app.get("/api/health", tags=["meta"])
    def health() -> dict[str, str]:
        """Liveness probe.

        Returns:
            Tool name and version.
        """
        return {"status": "ok", "tool": "docqc", "version": __version__}

    @app.get("/api/checkers", response_model=list[CheckerInfo], tags=["meta"])
    def list_checkers() -> list[CheckerInfo]:
        """Every registered checker, in execution order.

        Returns:
            Checker descriptors for the UI's checkbox list.
        """
        return [
            CheckerInfo(name=e.name, description=e.description, order=e.order)
            for e in registered()
        ]

    @app.get("/api/backends", response_model=list[BackendInfo], tags=["meta"])
    def list_backends() -> list[BackendInfo]:
        """Rendering backends available on this machine.

        Returns:
            Backend descriptors, so the UI can warn before a run is attempted
            that will silently skip the layout checker.
        """
        from ..render import detect_backends

        return [
            BackendInfo(name=b.name, available=b.available, detail=b.detail)
            for b in detect_backends()
        ]

    # -- jobs -------------------------------------------------------------

    @app.post(
        "/api/jobs",
        response_model=JobCreated,
        responses={400: {"model": ErrorResponse}, 413: {"model": ErrorResponse}},
        tags=["jobs"],
    )
    async def create_job(
        file: UploadFile = File(..., description="要检查的 .docx"),  # noqa: B008
        checkers: str | None = Form(  # noqa: B008
            default=None,
            description="逗号分隔的检查器名称；留空表示全部",
        ),
    ) -> JobCreated:
        """Accept a document and start checking it.

        Args:
            file: The uploaded ``.docx``.
            checkers: Optional comma-separated checker names.

        Returns:
            The new job's id and event-stream URL.

        Raises:
            HTTPException: 400 for a bad type or unknown checker, 413 for an
                oversized upload.
        """
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件类型 {suffix or '(无扩展名)'}；只接受 .docx",
            )

        content = await file.read()
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"文件过大：{len(content) / 1_048_576:.1f} MiB，"
                f"上限 {MAX_UPLOAD_BYTES / 1_048_576:.0f} MiB",
            )
        if not content:
            raise HTTPException(status_code=400, detail="文件为空")

        selected: list[str] | None = None
        if checkers and checkers.strip():
            selected = [name.strip() for name in checkers.split(",") if name.strip()]
            known = {e.name for e in registered()}
            unknown = [name for name in selected if name not in known]
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail=f"未知的检查器：{', '.join(unknown)}；"
                    f"可用：{', '.join(sorted(known))}",
                )

        job = store.create(file.filename or "upload.docx", content)
        store.start(job, selected)
        return JobCreated(
            job_id=job.job_id,
            filename=job.filename,
            size_bytes=len(content),
            checkers=selected or [e.name for e in registered()],
            events_url=f"/api/jobs/{job.job_id}/events",
        )

    @app.get(
        "/api/jobs/{job_id}",
        response_model=JobStatus,
        responses={404: {"model": ErrorResponse}},
        tags=["jobs"],
    )
    def get_job(job_id: str) -> JobStatus:
        """Poll a job's state, for clients without SSE.

        Args:
            job_id: Job identifier.

        Returns:
            The current state, including the report once finished.

        Raises:
            HTTPException: 404 when the job is unknown or evicted.
        """
        job = store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"找不到任务 {job_id}")
        return JobStatus(
            job_id=job.job_id,
            state=job.state,
            filename=job.filename,
            completed=job.completed,
            total=job.total,
            error=job.error,
            report=job.report,
        )

    @app.get(
        "/api/jobs/{job_id}/events",
        responses={404: {"model": ErrorResponse}},
        tags=["jobs"],
    )
    def job_events(job_id: str) -> StreamingResponse:
        """Follow a job over Server-Sent Events.

        SSE rather than WebSocket: progress is strictly server-to-client, a
        one-way stream, and SSE reconnects on its own without extra client code.

        Args:
            job_id: Job identifier.

        Returns:
            A ``text/event-stream`` response that ends after the terminal event.

        Raises:
            HTTPException: 404 when the job is unknown or evicted.
        """
        job = store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"找不到任务 {job_id}")

        def generate() -> Iterator[str]:
            """Yield SSE frames for the job.

            A ``: keep-alive`` comment is sent whenever the job is silent for
            :data:`STREAM_TIMEOUT_SECONDS`. This matters because a layout check
            can sit inside Word for minutes with nothing to report, and an idle
            connection is dropped by browsers and proxies alike.

            The store's generator yields ``None`` on a timeout tick, which is how
            the keep-alive is driven without a second thread.

            Yields:
                SSE frames.
            """
            # A client that subscribes late (a page reload mid-run) must still
            # get a terminal event, so the finished state is replayed.
            if job.state in {STATE_DONE, STATE_FAILED}:
                if job.state == STATE_DONE:
                    status = (job.report or {}).get("summary", {}).get("status", "?")
                    payload = {
                        "kind": "finished",
                        "message": f"检查完成：{status}",
                        "total": job.total,
                        "report": job.report,
                    }
                else:
                    payload = {
                        "kind": "failed",
                        "message": job.error or "检查失败",
                        "total": job.total,
                    }
                yield f"event: message\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                return

            for event in store.events(job, timeout=STREAM_TIMEOUT_SECONDS):
                if event is None:
                    yield ": keep-alive\n\n"
                    continue
                yield f"event: message\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                # Nginx and friends buffer responses by default, which defeats
                # streaming entirely.
                "X-Accel-Buffering": "no",
            },
        )

    @app.delete(
        "/api/jobs/{job_id}",
        responses={404: {"model": ErrorResponse}},
        tags=["jobs"],
    )
    def delete_job(job_id: str) -> dict[str, str]:
        """Drop a job and delete its upload.

        Deleting the file is best-effort. On Windows a worker that is mid-run
        holds the ``.docx`` open, so ``unlink`` can fail with WinError 32; that
        is a race, not a client error, and turning it into a 500 would mean a
        user cannot cancel a slow job. The job is marked for removal either way
        and its path is unlinked by eviction once the run settles.

        Args:
            job_id: Job identifier.

        Returns:
            A confirmation payload naming whether the file went immediately.

        Raises:
            HTTPException: 404 when the job is unknown.
        """
        job = store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"找不到任务 {job_id}")

        removed = "deleted"
        try:
            job.path.unlink(missing_ok=True)
        except PermissionError:
            # Held open by the running checker; shorten the job's life so the
            # next eviction sweep removes it.
            job.created_at = 0.0
            removed = "deferred"

        return {"status": removed, "job_id": job_id}

    # -- static frontend --------------------------------------------------

    # Mounted last so the API routes above always win. A single-page app needs
    # every non-API path to fall through to index.html rather than 404, which is
    # what `html=True` does — without it a browser refresh on any client-side
    # route fails. `docqc serve` is then one command for the whole product.
    assets = static_dir if static_dir is not None else default_static_dir()
    if assets is not None and (assets / "index.html").is_file():
        app.mount("/", StaticFiles(directory=assets, html=True), name="ui")
        app.state.static_dir = assets
    else:
        app.state.static_dir = None

    return app


def summarize_for_ui(report: dict[str, Any]) -> dict[str, Any]:
    """Derive the small summary the UI shows above the findings list.

    Args:
        report: A serialized report.

    Returns:
        Counts and the overall status, plus a plain-language verdict.
    """
    summary = report.get("summary", {})
    status = summary.get("status", Status.PASS.value)
    verdict = {
        Status.PASS.value: "没有发现问题",
        Status.WARN.value: "有问题建议修一下",
        Status.FAIL.value: "有必须修的问题",
        Status.ERROR.value: "有检查器运行失败",
    }.get(status, status)
    return {
        "status": status,
        "verdict": verdict,
        "errors": summary.get("errors", 0),
        "warnings": summary.get("warnings", 0),
        "infos": summary.get("infos", 0),
        "checkers_run": summary.get("checkers_run", 0),
        "checkers_errored": summary.get("checkers_errored", 0),
    }


def default_static_dir() -> Path | None:
    """Locate the built frontend, if it exists.

    Checked in two places because docqc runs both from a source checkout and
    from an installed wheel:

    * next to the package (``src/docqc/web/app.py`` -> ``<root>/frontend/dist``)
    * one level above the package (installed layout)

    Returns:
        The directory holding ``index.html``, or ``None`` when the frontend has
        not been built. Returning ``None`` rather than raising keeps the API
        usable on its own, which is what CI needs.
    """
    candidates = [
        Path(__file__).resolve().parents[3] / "frontend" / "dist",
        Path(__file__).resolve().parents[2] / "frontend" / "dist",
    ]
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


__all__ = [
    "ALLOWED_SUFFIXES",
    "MAX_UPLOAD_BYTES",
    "STREAM_TIMEOUT_SECONDS",
    "create_app",
    "default_static_dir",
]
