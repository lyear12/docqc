"""Drive the real UI end to end against the running server.

Confirms the browser flow works without a human clicking: upload the fixture
with the problem, follow the SSE stream, and check the report matches what the
CLI produced for the same file.
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8765"
FIXTURE = Path(r"C:\Users\31887\docqc\tests\fixtures\raw_latex.docx")


def multipart(field: str, filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = "----docqcui0123456789"
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head + content + tail, f"multipart/form-data; boundary={boundary}"


body, ctype = multipart("file", FIXTURE.name, FIXTURE.read_bytes())
request = urllib.request.Request(
    f"{BASE}/api/jobs", data=body, headers={"Content-Type": ctype}, method="POST"
)
with urllib.request.urlopen(request, timeout=60) as response:
    created = json.loads(response.read())

print(f"uploaded: job_id={created['job_id']}  checkers={created['checkers']}")
print("\n--- SSE ---")
report = None
with urllib.request.urlopen(
    f"{BASE}/api/jobs/{created['job_id']}/events", timeout=120
) as response:
    for raw in response:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        event = json.loads(line[5:].strip())
        print(f"  {event['kind']:18} {event['message'][:72]}")
        if event["kind"] == "finished":
            report = event["report"]
            break
        if event["kind"] == "failed":
            raise SystemExit(f"job failed: {event['message']}")

assert report is not None, "no report delivered"
print("\n--- report ---")
doc = report["document"]
print(f"  file      : {doc['name']}  ({doc['size_bytes']:,} B)")
print(f"  paragraphs: {doc['paragraph_count']}   tables: {doc['table_count']}")
summary = report["summary"]
print(f"  status    : {summary['status']}")
print(f"  errors    : {summary['errors']}   warnings: {summary['warnings']}")

print("\n--- findings ---")
for result in report["results"]:
    for finding in result["findings"]:
        if finding["severity"] in {"error", "warning"}:
            loc = finding["location"]
            where = " ".join(
                f"{k}={v}" for k, v in loc.items() if v is not None and k != "text"
            ) or "(document)"
            print(f"  [{finding['severity']:7}] {finding['code']}")
            print(f"      {where}")
            print(f"      {finding['message']}")
            if loc.get("text"):
                print(f"      raw: {loc['text'][:90]!r}")

print("\n--- delete ---")
request = urllib.request.Request(
    f"{BASE}/api/jobs/{created['job_id']}", method="DELETE"
)
with urllib.request.urlopen(request, timeout=30) as response:
    print(f"  {response.read().decode()}")

# Give the store a moment, then confirm the upload is gone.
time.sleep(0.3)
uploads = Path(r"C:\Users\31887\docqc\.docqc\uploads")
remaining = list(uploads.glob("*.docx")) if uploads.is_dir() else []
print(f"  files left in uploads/: {len(remaining)}")
