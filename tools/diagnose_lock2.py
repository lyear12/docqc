"""Find what actually holds the upload open during a web job.

The isolated python-docx test did NOT reproduce the lock, so the culprit is
somewhere else on the check path. Candidates, in the order a run touches them:

1. ``Document.render()`` -> Word COM (a separate process holding the file)
2. PyMuPDF (``fitz.open``) on the produced PDF
3. The upload's own file handle inside FastAPI's ``UploadFile``

This walks each one and reports whether the source .docx can be deleted after.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from docqc.document import Document

SRC = Path(r"C:\Users\31887\docqc\tests\fixtures\healthy.docx")


def probe(label: str, target: Path) -> bool:
    """Try to delete a file.

    Args:
        label: What was just done.
        target: File to delete.

    Returns:
        True when the delete succeeded.
    """
    try:
        target.unlink()
        print(f"  {label:44} deleted OK")
        return True
    except PermissionError as exc:
        print(f"  {label:44} BLOCKED (winerror={exc.winerror})")
        return False


def fresh(tmp: Path, name: str) -> Path:
    """Copy the source fixture to a scratch path.

    Args:
        tmp: Scratch directory.
        name: Base name for the copy.

    Returns:
        The copied path.
    """
    path = tmp / f"{name}.docx"
    shutil.copy(SRC, path)
    return path


tmp = Path(tempfile.mkdtemp(prefix="docqc-lock2-"))

print("=== A. 只解析段落，不渲染 ===")
a = fresh(tmp, "parse_only")
doc = Document(a)
_ = doc.paragraphs
doc.close()
probe("after Document.close() (parse only)", a)

print("\n=== B. 走 render()（Word COM -> PDF -> PNG）===")
b = fresh(tmp, "rendered")
doc = Document(b)
rendered = None
try:
    rendered = doc.render(dpi=72)
    print(f"  rendered {len(rendered.page_images)} page(s) -> {rendered.pdf_path.name}")
except Exception as exc:  # noqa: BLE001 - the point is to see the lock either way
    print(f"  render failed: {type(exc).__name__}: {str(exc)[:120]}")
doc.close()
probe("after render + close", b)

print("\n=== C. 渲染产物本身能否删 ===")
if rendered is not None:
    probe("pdf", rendered.pdf_path)
    for image in rendered.page_images[:1]:
        probe(f"png {image.name}", image)

print("\n=== D. Word COM 进程是否残留 ===")
import subprocess

result = subprocess.run(
    ["tasklist", "/FI", "IMAGENAME eq WINWORD.EXE", "/NH"],
    capture_output=True,
    text=True,
    errors="replace",
    check=False,
)
print(f"  {result.stdout.strip()[:200]}")

shutil.rmtree(tmp, ignore_errors=True)
