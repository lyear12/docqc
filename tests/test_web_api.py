"""HTTP API behaviour.

These tests drive the app through FastAPI's TestClient, so they assert the real
request/response cycle rather than calling handlers directly. The uploaded bytes
are a generated fixture, which keeps the tests independent of any file on the
developer's machine.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.make_fixtures import build_all

from docqc.web.app import MAX_UPLOAD_BYTES, create_app


@pytest.fixture(scope="module")
def fixture_paths() -> dict[str, Path]:
    """Generated .docx fixtures, shared across the module.

    Returns:
        Fixture name to path.
    """
    return build_all()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """A TestClient backed by a throwaway upload directory.

    Yields:
        The client, already entered so startup/shutdown hooks run.
    """
    app = create_app(work_dir=tmp_path / "uploads")
    with TestClient(app) as test_client:
        yield test_client
    app.state.store.purge()


def upload(client: TestClient, path: Path, **form: str) -> dict:
    """POST a fixture and return the JSON response.

    Args:
        client: Test client.
        path: File to upload.
        **form: Extra form fields.

    Returns:
        Parsed response body.
    """
    with path.open("rb") as handle:
        response = client.post(
            "/api/jobs",
            files={
                "file": (
                    path.name,
                    handle,
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document",
                )
            },
            data=form,
        )
    assert response.status_code == 200, response.text
    return response.json()


def wait_for(
    client: TestClient, job_id: str, *, timeout: float = 30.0
) -> dict:
    """Poll a job until it settles.

    The run happens on a worker thread, so a tight polling loop would spin
    through its attempt budget before the checker had started. Sleeping between
    attempts is what makes this a wait rather than a race.

    Args:
        client: Test client.
        job_id: Job to follow.
        timeout: Seconds to wait before giving up.

    Returns:
        The final status payload.
    """
    deadline = time.monotonic() + timeout
    status = client.get(f"/api/jobs/{job_id}").json()
    while status["state"] not in {"done", "failed"} and time.monotonic() < deadline:
        time.sleep(0.05)
        status = client.get(f"/api/jobs/{job_id}").json()
    return status


# -- metadata -----------------------------------------------------------------


def test_health(client: TestClient) -> None:
    """The liveness probe reports the tool version."""
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["tool"] == "docqc"


def test_checkers_are_listed(client: TestClient) -> None:
    """Every registered checker is advertised, in execution order."""
    checkers = client.get("/api/checkers").json()
    names = [c["name"] for c in checkers]
    assert {"layout", "formula", "structure"} <= set(names)
    orders = [c["order"] for c in checkers]
    assert orders == sorted(orders)


def test_backends_are_listed(client: TestClient) -> None:
    """Backend availability is reported without raising on a bare machine."""
    backends = client.get("/api/backends").json()
    assert {b["name"] for b in backends} >= {"word", "libreoffice"}
    assert all(isinstance(b["available"], bool) for b in backends)


# -- upload validation --------------------------------------------------------


def test_rejects_wrong_extension(client: TestClient, tmp_path: Path) -> None:
    """A .txt upload is refused before anything is written."""
    bad = tmp_path / "notes.txt"
    bad.write_text("hello", encoding="utf-8")
    response = client.post("/api/jobs", files={"file": ("notes.txt", bad.read_bytes())})
    assert response.status_code == 400
    assert ".docx" in response.json()["detail"]


def test_rejects_empty_file(client: TestClient) -> None:
    """A zero-byte upload is refused."""
    response = client.post("/api/jobs", files={"file": ("empty.docx", b"")})
    assert response.status_code == 400


def test_rejects_oversized_file(client: TestClient) -> None:
    """An upload over the cap is refused with the limit in the message."""
    oversized = b"x" * (MAX_UPLOAD_BYTES + 1)
    response = client.post("/api/jobs", files={"file": ("big.docx", oversized)})
    assert response.status_code == 413
    assert "文件过大" in response.json()["detail"]


def test_rejects_unknown_checker(client: TestClient, fixture_paths: dict[str, Path]) -> None:
    """Naming a checker that does not exist is a client error, not a silent skip."""
    with fixture_paths["healthy"].open("rb") as handle:
        response = client.post(
            "/api/jobs",
            files={"file": ("healthy.docx", handle)},
            data={"checkers": "structure,nope"},
        )
    assert response.status_code == 400
    assert "nope" in response.json()["detail"]


def test_upload_path_cannot_escape_work_dir(
    client: TestClient, fixture_paths: dict[str, Path]
) -> None:
    """A traversal attempt in the filename must not write outside the store.

    The stored name is derived from the job id; the client's name is kept only
    for display.
    """
    with fixture_paths["healthy"].open("rb") as handle:
        response = client.post(
            "/api/jobs",
            files={"file": ("../../escaped.docx", handle)},
        )
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    job = client.app.state.store.get(job_id)  # type: ignore[attr-defined]
    assert job is not None
    work_dir = client.app.state.store.work_dir  # type: ignore[attr-defined]
    assert job.path.parent == work_dir
    assert "escaped" not in job.path.name


# -- running ------------------------------------------------------------------


def test_job_runs_and_reports(client: TestClient, fixture_paths: dict[str, Path]) -> None:
    """A full upload settles into a finished report with the expected findings."""
    created = upload(client, fixture_paths["raw_latex"])
    status = wait_for(client, created["job_id"])
    assert status["state"] == "done", status

    report = status["report"]
    assert report is not None
    codes = {
        finding["code"]
        for result in report["results"]
        for finding in result["findings"]
    }
    assert "RAW_LATEX_IN_TEXT" in codes


def test_healthy_document_reports_pass(
    client: TestClient, fixture_paths: dict[str, Path]
) -> None:
    """The web path agrees with the CLI: a clean file passes.

    Runs the render-free checkers only. Including ``layout`` would drive Word
    through COM for one page, which costs seconds and makes this test's runtime
    depend on whether Word happens to be installed.
    """
    created = upload(client, fixture_paths["healthy"], checkers="structure,formula")
    status = wait_for(client, created["job_id"])
    assert status["state"] == "done", status
    assert status["report"]["summary"]["status"] == "pass"


def test_subset_of_checkers(client: TestClient, fixture_paths: dict[str, Path]) -> None:
    """Restricting the run to one checker runs only that checker."""
    created = upload(client, fixture_paths["raw_latex"], checkers="structure")
    status = wait_for(client, created["job_id"])
    results = status["report"]["results"]
    assert [r["checker"] for r in results] == ["structure"]


# -- streaming ----------------------------------------------------------------


def test_events_stream_terminates(client: TestClient, fixture_paths: dict[str, Path]) -> None:
    """The SSE stream delivers progress frames and stops after the terminal one.

    The stream must end on its own; a client that never receives a terminal
    event would spin forever showing "running".
    """
    created = upload(client, fixture_paths["healthy"], checkers="structure")
    kinds: list[str] = []
    with client.stream("GET", f"/api/jobs/{created['job_id']}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            payload = json.loads(line[len("data:") :].strip())
            kinds.append(payload["kind"])
            if payload["kind"] in {"finished", "failed"}:
                break

    assert kinds[0] == "started"
    assert kinds[-1] in {"finished", "failed"}
    assert "checker_finished" in kinds


def test_unknown_job_is_404(client: TestClient) -> None:
    """Polling and streaming an unknown id both report 404."""
    assert client.get("/api/jobs/deadbeef").status_code == 404
    assert client.get("/api/jobs/deadbeef/events").status_code == 404


def test_delete_removes_the_upload(client: TestClient, fixture_paths: dict[str, Path]) -> None:
    """Deleting a job removes its file from disk.

    The job is waited for first: deleting *during* a run races the worker, which
    on Windows still holds the file open inside Word. The API's contract is about
    a settled job, so that is what is asserted.
    """
    created = upload(client, fixture_paths["healthy"], checkers="structure")
    job_id = created["job_id"]
    wait_for(client, job_id)

    job = client.app.state.store.get(job_id)  # type: ignore[attr-defined]
    assert job is not None
    assert job.path.is_file()

    assert client.delete(f"/api/jobs/{job_id}").status_code == 200
    assert not job.path.is_file()


def test_delete_while_running_does_not_crash(
    client: TestClient, fixture_paths: dict[str, Path]
) -> None:
    """Deleting mid-run is accepted and does not produce a 500.

    A worker inside Word holds the ``.docx`` open, so ``unlink`` can fail with
    WinError 32. That is a race, not a client error: the request must still
    succeed, reporting that removal was deferred.
    """
    created = upload(client, fixture_paths["healthy"], checkers="structure")
    response = client.delete(f"/api/jobs/{created['job_id']}")
    assert response.status_code == 200
    assert response.json()["status"] in {"deleted", "deferred"}
    wait_for(client, created["job_id"])


def test_summarize_for_ui_speaks_plainly() -> None:
    """The UI summary turns a status code into a sentence a person can read."""
    from docqc.web.app import summarize_for_ui

    summary = summarize_for_ui(
        {"summary": {"status": "fail", "errors": 2, "warnings": 1, "infos": 0}}
    )
    assert summary["verdict"] == "有必须修的问题"
    assert summary["errors"] == 2
