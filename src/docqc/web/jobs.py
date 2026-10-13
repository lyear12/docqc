"""In-memory job store with a per-job event stream.

Why jobs at all: a layout check drives Word through COM and rasterizes every
page, which takes seconds to minutes. An HTTP request that blocks for that long
would time out in the browser and give the user no feedback. So an upload hands
back an id immediately, the work runs in a background thread, and progress is
pushed over a queue the SSE endpoint drains.

Why in-memory rather than a database: this is a single-user local tool. A job
lives as long as the process, its uploaded file is deleted when the job is
evicted, and nothing is written outside ``.docqc/``. That keeps the deployment
story to "run one command" and means a crashed run leaves no state to reconcile.

Concurrency: one :class:`threading.Lock` guards the store; each job owns its own
:class:`queue.Queue`. Checkers run on a worker thread, so a slow render never
blocks the event loop that is serving other requests.
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import Status
from ..registry import registered
from ..runner import check

#: How many finished jobs to keep. Old ones are evicted with their files, so a
#: long-lived server does not accumulate uploads forever.
MAX_JOBS = 32

#: A job's states, in order.
STATE_PENDING = "pending"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"

#: Sentinel pushed onto a job's queue to tell the SSE generator to stop.
_STREAM_END = object()


@dataclass
class Job:
    """One checking run and everything a client needs to follow it.

    Attributes:
        job_id: Opaque identifier handed to the client.
        filename: Original upload name, echoed in the report.
        path: Where the upload was written; deleted on eviction.
        state: See the ``STATE_*`` constants.
        completed: How many checkers have finished.
        total: How many checkers this run will execute.
        error: Failure text when ``state`` is ``STATE_FAILED``.
        report: The finished report as a plain dict.
        created_at: Unix time, used for eviction order.
        queue: Events for the SSE endpoint; receives ``_STREAM_END`` when done.
        subscribers: How many SSE readers are attached, so a finished job is not
            evicted while someone is still reading it.
    """

    job_id: str
    filename: str
    path: Path
    state: str = STATE_PENDING
    completed: int = 0
    total: int = 0
    error: str | None = None
    report: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    queue: queue.Queue[Any] = field(default_factory=queue.Queue)
    subscribers: int = 0


class JobStore:
    """Thread-safe registry of jobs, with background execution.

    Args:
        work_dir: Directory to write uploads into.
        keep: Maximum number of jobs to retain.
    """

    def __init__(self, work_dir: Path, *, keep: int = MAX_JOBS) -> None:
        self.work_dir = work_dir
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._keep = keep
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    # -- lifecycle --------------------------------------------------------

    def create(self, filename: str, content: bytes) -> Job:
        """Store an upload and register a pending job.

        Args:
            filename: Original name, used for the report and as a hint only.
            content: Raw bytes of the uploaded document.

        Returns:
            The registered job, still pending.
        """
        job_id = uuid.uuid4().hex[:16]
        # Never trust the client's filename for a path: a name like
        # ``../../x.docx`` would escape the work directory. Only the suffix is
        # kept, and the stored name is derived from the job id.
        suffix = Path(filename).suffix.lower() or ".docx"
        path = self.work_dir / f"{job_id}{suffix}"
        path.write_bytes(content)

        job = Job(job_id=job_id, filename=Path(filename).name, path=path)
        job.total = len(registered())
        with self._lock:
            self._jobs[job_id] = job
            self._evict_locked()
        return job

    def get(self, job_id: str) -> Job | None:
        """Look up a job.

        Args:
            job_id: Identifier from :meth:`create`.

        Returns:
            The job, or ``None`` when unknown or evicted.
        """
        with self._lock:
            return self._jobs.get(job_id)

    def _evict_locked(self) -> None:
        """Drop the oldest finished jobs and delete their files.

        Must be called with the lock held. Running jobs and jobs with a live SSE
        subscriber are never evicted.
        """
        if len(self._jobs) <= self._keep:
            return
        candidates = sorted(self._jobs.values(), key=lambda j: j.created_at)
        for job in candidates:
            if len(self._jobs) <= self._keep:
                break
            if job.state in {STATE_PENDING, STATE_RUNNING} or job.subscribers > 0:
                continue
            job.path.unlink(missing_ok=True)
            self._jobs.pop(job.job_id, None)

    def purge(self) -> None:
        """Delete every job and its uploaded file. For shutdown and tests."""
        with self._lock:
            for job in self._jobs.values():
                job.path.unlink(missing_ok=True)
            self._jobs.clear()

    # -- execution --------------------------------------------------------

    def start(self, job: Job, checkers: list[str] | None = None) -> None:
        """Run the checkers for a job on a worker thread.

        Args:
            job: A job from :meth:`create`.
            checkers: Restrict to these checker names; ``None`` runs all.
        """
        thread = threading.Thread(
            target=self._run, args=(job, checkers), name=f"docqc-{job.job_id}", daemon=True
        )
        thread.start()

    def _run(self, job: Job, checkers: list[str] | None) -> None:
        """Execute the checkers and publish every event.

        Exceptions are caught and turned into a ``failed`` event rather than
        killing the thread silently — a client waiting on the stream must always
        receive a terminal event.

        Args:
            job: The job to run.
            checkers: Checker names, or ``None`` for all.
        """
        job.state = STATE_RUNNING
        self._emit(job, "started", f"开始检查 {job.filename}", total=job.total)

        def progress(name: str, index: int, total: int) -> None:
            self._emit(
                job,
                "checker_started",
                f"正在运行 {name}（{index}/{total}）",
                checker=name,
                index=index,
                total=total,
            )

        try:
            report = check(job.path, names=checkers, progress=progress)
        except Exception as exc:  # noqa: BLE001 - a failed job must terminate
            job.state = STATE_FAILED
            job.error = f"{type(exc).__name__}: {exc}"
            self._emit(job, "failed", job.error)
            self._close(job)
            return

        for result in report.results:
            job.completed += 1
            detail = ""
            if result.status is Status.ERROR:
                detail = f"（检查器出错：{result.error or '未知原因'}）"
            elif result.findings:
                counts = result.counts
                detail = (
                    f"（{counts['error']} 错误 / {counts['warning']} 警告 /"
                    f" {counts['info']} 提示）"
                )
            self._emit(
                job,
                "checker_finished",
                f"{result.checker} 完成 {detail}".strip(),
                checker=result.checker,
                index=job.completed,
                total=job.total,
            )

        job.report = report.model_dump(mode="json")
        job.state = STATE_DONE
        self._emit(
            job,
            "finished",
            f"检查完成：{report.summary.status.value}",
            total=job.total,
            report=job.report,
        )
        self._close(job)

    def _emit(self, job: Job, kind: str, message: str, **extra: Any) -> None:
        """Publish one event to a job's queue.

        Args:
            job: The job to publish on.
            kind: Event kind.
            message: Human-readable line.
            **extra: Additional event fields.
        """
        job.queue.put({"kind": kind, "message": message, **extra})

    @staticmethod
    def _close(job: Job) -> None:
        """Push the stream terminator so readers stop waiting.

        Args:
            job: The finished job.
        """
        job.queue.put(_STREAM_END)

    # -- streaming --------------------------------------------------------

    def events(
        self, job: Job, *, timeout: float | None = None
    ) -> Iterator[dict[str, Any] | None]:
        """Yield events for a job until it terminates.

        Args:
            job: The job to follow.
            timeout: Seconds to wait for the next event before yielding
                ``None`` as a keep-alive signal. ``None`` blocks forever.

        Yields:
            Event dictionaries, or ``None`` on a keep-alive tick. Ends after
            ``finished`` or ``failed``.

        Notes:
            The subscriber count is held for the duration so eviction cannot
            delete a file that is still being reported on, and the terminal
            sentinel is re-queued so a second subscriber also sees it stop.
        """
        with self._lock:
            job.subscribers += 1
        try:
            while True:
                try:
                    item = job.queue.get(timeout=timeout)
                except queue.Empty:
                    yield None
                    continue
                if item is _STREAM_END:
                    # Put it back: several clients may follow one job, and each
                    # must observe the terminator rather than block forever.
                    job.queue.put(_STREAM_END)
                    return
                yield item
        finally:
            with self._lock:
                job.subscribers -= 1


__all__ = [
    "MAX_JOBS",
    "STATE_DONE",
    "STATE_FAILED",
    "STATE_PENDING",
    "STATE_RUNNING",
    "Job",
    "JobStore",
]
