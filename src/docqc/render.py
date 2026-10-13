"""Render a document to PDF, then to one PNG per page.

This is the expensive half of the tool: it shells out to Word (via COM) and to
poppler. Two consequences shape the design.

**Platform dependence is real.** Word COM exists only on Windows with Word
installed; poppler is a separate download. So rendering is optional and
detected at runtime rather than assumed. When no backend is available the
layout checker reports ``SKIPPED`` with a reason instead of failing — a text-only
check is still worth running.

**Failures must be diagnosable.** When a backend is present but a specific file
will not convert, the error names the backend, the file, and the exit status.
"It didn't work" is useless when the user has 886 pages to get through.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .document import RenderResult

#: Where docqc keeps intermediate artifacts. Everything here is disposable.
WORK_DIR_NAME = ".docqc"


class RenderUnavailableError(RuntimeError):
    """No usable rendering backend exists on this machine."""


@dataclass(frozen=True, slots=True)
class Backend:
    """A way to turn a document into a PDF.

    Attributes:
        name: Short identifier, used in messages and findings.
        available: Whether the executable/COM interface was found.
        detail: Human-readable explanation of the availability verdict.
    """

    name: str
    available: bool
    detail: str


def _word_available() -> Backend:
    """Check whether Word can be driven through COM.

    Returns:
        A :class:`Backend`; ``available`` is False on non-Windows, without Word,
        or when COM is blocked by policy.
    """
    import sys

    if sys.platform != "win32":
        return Backend("word", False, "Word COM is Windows-only")
    try:
        import win32com.client
    except ImportError:
        return Backend("word", False, "pywin32 is not installed")
    try:
        app = win32com.client.Dispatch("Word.Application")
    except Exception as exc:  # noqa: BLE001 - COM raises many unrelated types
        return Backend("word", False, f"Word is not available: {exc}")
    with contextlib.suppress(Exception):
        app.Quit()
    return Backend("word", True, "Word COM is available")


def _soffice_path() -> Path | None:
    """Locate the LibreOffice headless binary.

    LibreOffice is the fallback when Word is absent. It is looked up on ``PATH``
    first and then in the standard install locations, because the installer does
    not always add it to ``PATH``.

    Returns:
        Path to ``soffice``, or ``None``.
    """
    found = shutil.which("soffice") or shutil.which("soffice.exe")
    if found:
        return Path(found)
    candidates = [
        Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
        Path(r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def detect_backends() -> list[Backend]:
    """Every PDF backend this machine could use, with availability.

    Returns:
        Backends in preference order: Word first (best fidelity), LibreOffice
        second.
    """
    soffice = _soffice_path()
    return [
        _word_available(),
        Backend(
            "libreoffice",
            soffice is not None,
            f"found at {soffice}" if soffice else "soffice executable not found",
        ),
    ]


def _load_pymupdf() -> Any | None:
    """Import PyMuPDF, tolerating both module names.

    ``pymupdf`` is the current name; ``fitz`` is the historical one and still
    works in 1.28. Importing ``fitz`` prints a deprecation warning, so the new
    name is tried first.

    The return type is ``Any`` deliberately: PyMuPDF's own annotations are
    incomplete, and mypy reports its constructors as untyped calls. Confining
    that to one boundary function keeps the rest of the module strictly typed
    instead of spreading ``type: ignore`` through the rasterization loop.

    Returns:
        The module, or ``None`` when PyMuPDF is not installed.
    """
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore[no-redef]
        except ImportError:
            return None
    return pymupdf


def _rasterize_with_pymupdf(
    pdf: Path, out_dir: Path, dpi: int
) -> tuple[list[Path], list[tuple[int, int]]]:
    """Rasterize a PDF using PyMuPDF.

    Args:
        pdf: Source PDF.
        out_dir: Directory for the page images.
        dpi: Resolution.

    Returns:
        ``(image_paths, sizes)`` in page order.

    Raises:
        RenderUnavailableError: When PyMuPDF is not installed.
    """
    module = _load_pymupdf()
    if module is None:
        raise RenderUnavailableError("PyMuPDF is not installed")

    images: list[Path] = []
    sizes: list[tuple[int, int]] = []
    zoom = dpi / 72.0
    with module.open(pdf) as doc:
        for number, page in enumerate(doc, start=1):
            pixmap = page.get_pixmap(matrix=module.Matrix(zoom, zoom))
            target = out_dir / f"page-{number:04d}.png"
            pixmap.save(str(target))
            images.append(target)
            sizes.append((int(pixmap.width), int(pixmap.height)))
    return images, sizes


def _pdf_to_png(pdf: Path, out_dir: Path, dpi: int) -> tuple[list[Path], list[tuple[int, int]]]:
    """Rasterize a PDF to one PNG per page.

    Prefers PyMuPDF (pure wheel, no external binary) and falls back to poppler's
    ``pdftoppm``.

    Args:
        pdf: Source PDF.
        out_dir: Directory to write ``page-0001.png`` and friends into.
        dpi: Resolution.

    Returns:
        ``(image_paths, sizes)`` — sizes are ``(width, height)`` in pixels.

    Raises:
        RenderUnavailableError: If neither rasterizer is usable.
    """
    if _load_pymupdf() is not None:
        return _rasterize_with_pymupdf(pdf, out_dir, dpi)

    pdftoppm = shutil.which("pdftoppm") or shutil.which("pdftoppm.exe")
    if pdftoppm is None:
        raise RenderUnavailableError(
            "no PDF rasterizer available: install PyMuPDF (pip install PyMuPDF) "
            "or put poppler's pdftoppm on PATH"
        )
    prefix = out_dir / "page"
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [pdftoppm, "-png", "-r", str(dpi), str(pdf), str(prefix)],
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"pdftoppm failed for {pdf.name} (exit {completed.returncode}): "
            f"{completed.stderr.strip()[:400]}"
        )
    images = sorted(out_dir.glob("page-*.png"))
    sizes = []
    from PIL import Image

    for image in images:
        with Image.open(image) as handle:
            sizes.append((handle.width, handle.height))
    return images, sizes


def _docx_to_pdf_word(source: Path, target: Path) -> None:
    """Convert via Word COM.

    Args:
        source: ``.docx`` to convert.
        target: Destination PDF path.

    Raises:
        RuntimeError: When Word reports a failure.
    """
    import win32com.client

    app = win32com.client.Dispatch("Word.Application")
    app.Visible = False
    app.DisplayAlerts = 0
    document = None
    try:
        document = app.Documents.Open(str(source), ReadOnly=True)
        # 17 == wdExportFormatPDF
        document.ExportAsFixedFormat(str(target), 17)
    except Exception as exc:  # noqa: BLE001 - COM surfaces many types
        raise RuntimeError(f"Word failed to convert {source.name}: {exc}") from exc
    finally:
        if document is not None:
            with contextlib.suppress(Exception):
                document.Close(SaveChanges=0)
        with contextlib.suppress(Exception):
            app.Quit()


def _docx_to_pdf_soffice(source: Path, target: Path, soffice: Path) -> None:
    """Convert via LibreOffice headless.

    LibreOffice always writes ``<stem>.pdf`` into ``--outdir``, so the file is
    moved into place afterwards when the names differ.

    Args:
        source: ``.docx`` to convert.
        target: Destination PDF path.
        soffice: Path to the ``soffice`` executable.

    Raises:
        RuntimeError: When LibreOffice reports a failure or writes nothing.
    """
    out_dir = target.parent
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            str(soffice),
            "--headless",
            "--norestore",
            "--convert-to",
            "pdf",
            "--outdir",
            str(out_dir),
            str(source),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    produced = out_dir / f"{source.stem}.pdf"
    if not produced.is_file():
        raise RuntimeError(
            f"LibreOffice produced no PDF for {source.name} "
            f"(exit {completed.returncode}): {completed.stdout.strip()[:300]} "
            f"{completed.stderr.strip()[:300]}"
        )
    if produced != target:
        produced.replace(target)


def render_document(source: Path, *, dpi: int = 144, work_dir: Path | None = None) -> RenderResult:
    """Render ``source`` to PDF and rasterize it.

    Args:
        source: The document to render.
        dpi: Rasterization resolution.
        work_dir: Where to place artifacts. Defaults to ``<cwd>/.docqc/render``.

    Returns:
        The rendered pages.

    Raises:
        RenderUnavailableError: When no backend can convert this file.
        RuntimeError: When a backend exists but the conversion failed.
    """
    work_dir = work_dir or (Path.cwd() / WORK_DIR_NAME / "render")
    work_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = work_dir / f"{source.stem}.pdf"

    errors: list[str] = []
    backends = detect_backends()

    for backend in backends:
        if not backend.available:
            errors.append(f"{backend.name}: {backend.detail}")
            continue
        try:
            if backend.name == "word":
                _docx_to_pdf_word(source, pdf_path)
            else:
                soffice = _soffice_path()
                if soffice is None:
                    errors.append("libreoffice: executable vanished")
                    continue
                _docx_to_pdf_soffice(source, pdf_path, soffice)
        except Exception as exc:  # noqa: BLE001 - try the next backend
            errors.append(f"{backend.name}: {exc}")
            continue
        images, sizes = _pdf_to_png(pdf_path, work_dir, dpi)
        return RenderResult(
            pdf_path=pdf_path, page_images=images, page_sizes_px=sizes, dpi=dpi
        )

    detail = "; ".join(errors) if errors else "no backend reported a reason"
    raise RenderUnavailableError(f"cannot render {source.name}: {detail}")


def temp_work_dir() -> tempfile.TemporaryDirectory[str]:
    """A self-cleaning scratch directory for throwaway renders.

    Returns:
        A context manager whose ``name`` attribute is the directory path.
    """
    return tempfile.TemporaryDirectory(prefix="docqc-")


__all__ = [
    "Backend",
    "RenderUnavailableError",
    "detect_backends",
    "render_document",
    "temp_work_dir",
]
