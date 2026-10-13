"""Start the API on a real socket and exercise it end to end.

The pytest suite uses TestClient, which bypasses uvicorn and the actual HTTP
stack. This drives the running server the way a browser will: upload over
multipart, poll, and read the SSE stream.

Exits non-zero if any step fails, so it doubles as a smoke test.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn

from docqc.web.app import create_app

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
FIXTURE = Path(r"C:\Users\31887\docqc\tests\fixtures\raw_latex.docx")


def multipart(field: str, filename: str, content: bytes) -> tuple[bytes, str]:
    """Build a multipart/form-data body with one file part.

    Args:
        field: Form field name.
        filename: Name to report for the file.
        content: File bytes.

    Returns:
        ``(body, content_type)``.
    """
    boundary = "----docqcsmoke0123456789"
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head + content + tail, f"multipart/form-data; boundary={boundary}"


def check(label: str, ok: bool, detail: str = "") -> bool:
    """Print one assertion line.

    Args:
        label: What is being checked.
        ok: Whether it passed.
        detail: Extra context on failure.

    Returns:
        ``ok``.
    """
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f"  {detail}" if detail else ""))
    return ok


def main() -> int:
    """Serve the app, exercise it, and report.

    Returns:
        Process exit code.
    """
    app = create_app(work_dir=Path(r"C:\Users\31887\docqc\.docqc\smoke-uploads"))
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    if not server.started:
        print("server failed to start")
        return 1

    failures = 0
    try:
        print(f"=== server up on {BASE} ===")

        # -- metadata --
        with urllib.request.urlopen(f"{BASE}/api/health", timeout=10) as response:
            health = json.loads(response.read())
        failures += not check("GET /api/health", health.get("status") == "ok")

        with urllib.request.urlopen(f"{BASE}/api/checkers", timeout=10) as response:
            checkers = json.loads(response.read())
        failures += not check(
            "GET /api/checkers returns all three",
            {"layout", "formula", "structure"} <= {c["name"] for c in checkers},
            str([c["name"] for c in checkers]),
        )

        with urllib.request.urlopen(f"{BASE}/api/backends", timeout=30) as response:
            backends = json.loads(response.read())
        available = [b["name"] for b in backends if b["available"]]
        failures += not check(
            "GET /api/backends reports availability",
            len(backends) == 2,
            f"available: {available}",
        )

        # -- upload --
        body, content_type = multipart("file", FIXTURE.name, FIXTURE.read_bytes())
        request = urllib.request.Request(
            f"{BASE}/api/jobs",
            data=body,
            headers={"Content-Type": content_type},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            created = json.loads(response.read())
        job_id = created["job_id"]
        failures += not check("POST /api/jobs", bool(job_id), f"job_id={job_id}")

        # -- SSE --
        print("\n  --- SSE stream ---")
        events: list[dict] = []
        with urllib.request.urlopen(
            f"{BASE}/api/jobs/{job_id}/events", timeout=60
        ) as response:
            if response.headers.get("content-type", "").startswith("text/event-stream"):
                pass
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = json.loads(line[5:].strip())
                events.append(payload)
                print(f"    {payload['kind']:18} {payload['message'][:70]}")
                if payload["kind"] in {"finished", "failed"}:
                    break

        kinds = [e["kind"] for e in events]
        failures += not check("stream starts with 'started'", kinds[:1] == ["started"], str(kinds))
        failures += not check("stream ends with a terminal event", kinds[-1:] in (["finished"], ["failed"]))
        failures += not check("stream carried per-checker progress", "checker_finished" in kinds)

        final = events[-1] if events else {}
        report = final.get("report") or {}
        codes = {
            f["code"] for r in report.get("results", []) for f in r.get("findings", [])
        }
        failures += not check(
            "report contains RAW_LATEX_IN_TEXT",
            "RAW_LATEX_IN_TEXT" in codes,
            f"status={report.get('summary', {}).get('status')}",
        )

        # -- poll --
        with urllib.request.urlopen(f"{BASE}/api/jobs/{job_id}", timeout=10) as response:
            status = json.loads(response.read())
        failures += not check("GET /api/jobs/{id} agrees with the stream", status["state"] == "done", status["state"])

        # -- error paths --
        bad, ct = multipart("file", "x.txt", b"not a docx")
        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    f"{BASE}/api/jobs", data=bad, headers={"Content-Type": ct}, method="POST"
                ),
                timeout=10,
            )
            failures += not check("rejects .txt", False, "expected HTTP 400")
        except urllib.error.HTTPError as exc:
            failures += not check("rejects .txt with 400", exc.code == 400, f"got {exc.code}")

        try:
            urllib.request.urlopen(f"{BASE}/api/jobs/nope", timeout=10)
            failures += not check("unknown job is 404", False, "expected HTTP 404")
        except urllib.error.HTTPError as exc:
            failures += not check("unknown job is 404", exc.code == 404, f"got {exc.code}")

    finally:
        server.should_exit = True
        thread.join(timeout=10)
        app.state.store.purge()

    print()
    print(f"=== {'ALL PASSED' if failures == 0 else f'{failures} FAILURE(S)'} ===")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
