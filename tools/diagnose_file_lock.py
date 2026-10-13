"""Diagnose the WinError 32 on deleting a checked document.

Suspicion: `python-docx` keeps the .docx open as a zip, and dropping the
reference does not close it promptly because the ``Document`` object sits in a
reference cycle (package -> part -> package). CPython only collects cycles with
the cycle collector, which is not prompt enough for "delete right after check".

This reproduces it in isolation and tests whether an explicit close fixes it.
"""

from __future__ import annotations

import gc
import shutil
import tempfile
from pathlib import Path

import docx

SRC = Path(r"C:\Users\31887\docqc\tests\fixtures\healthy.docx")


def try_delete(label: str, path: Path) -> None:
    """Attempt to delete a file and report the outcome.

    Args:
        label: What this attempt represents.
        path: File to delete.
    """
    try:
        path.unlink()
        print(f"  {label:32} deleted OK")
    except PermissionError as exc:
        print(f"  {label:32} BLOCKED: {exc.winerror}")


tmp = Path(tempfile.mkdtemp(prefix="docqc-lock-"))
target = tmp / "probe.docx"
shutil.copy(SRC, target)

print("=== 1. python-docx 打开后直接丢引用 ===")
opened = docx.Document(str(target))
_ = opened.paragraphs
del opened
try_delete("after del (no gc)", target)

if target.exists():
    print("\n=== 2. 强制 gc.collect() 后再试 ===")
    gc.collect()
    try_delete("after gc.collect()", target)

print("\n=== 3. 检查 zip 句柄是否真的还在 ===")
if target.exists():
    reopen = docx.Document(str(target))
    pkg = reopen.part.package
    print(f"  package: {type(pkg).__name__}")
    zf = getattr(pkg, "_zipf", None)
    print(f"  has _zipf: {zf is not None}")
    if zf is not None:
        print(f"  zip closed before close(): {getattr(zf, 'fp', 'n/a') is None}")
    # python-docx 0.8.11+ 的 package 关闭方式
    closer = getattr(pkg, "close", None)
    print(f"  package.close exists: {callable(closer)}")
    if callable(closer):
        closer()
        try_delete("after package.close()", target)
    else:
        print("  -> no close(); need explicit gc or a different strategy")

print(f"\ntmp dir: {tmp}")
shutil.rmtree(tmp, ignore_errors=True)
