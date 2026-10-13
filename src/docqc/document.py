"""Format-neutral view of the document under inspection.

Checkers talk to :class:`Document`, never to ``python-docx`` directly. That
indirection buys three things:

* **Checkers stay testable.** A unit test can hand a checker a tiny in-memory
  ``Document`` instead of a real ``.docx`` on disk.
* **A second format is cheap.** Adding ``.pptx`` support means writing another
  loader, not touching any checker.
* **The lazy/open split is explicit.** Parsing is cheap; rendering to PDF and
  rasterizing pages is not. Only checkers that genuinely need pixels ask for
  them, so a text-only run stays fast.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import DocumentInfo
from .styles import ResolvedParagraphStyle, StyleTable


@dataclass(slots=True)
class Paragraph:
    """One paragraph, flattened to what checkers actually need.

    Attributes:
        index: 0-based position in the document body.
        text: The paragraph's text with runs concatenated.
        style_name: Localized style name (e.g. ``标题 1``); for display only.
        style_name_id: Raw ``w:pStyle`` value (e.g. ``Heading1``); this is what
            ``styles.xml`` is keyed by, so it is what inheritance resolves
            against. Word localizes names but never ids, so the two must be
            carried separately.
        outline_level: ``w:outlineLvl`` declared *directly on this paragraph*.
            ``None`` here does not mean "no outline level" — the value is
            usually inherited from the style. Use
            :meth:`Document.resolve_style` for the effective value.
        is_heading: True when the paragraph is a real heading by style name.
        runs_with_latex: Run texts that look like unrendered LaTeX.
        has_omml: True when the paragraph contains an OMML math element.
        raw: The underlying object, for checkers that need the XML.
    """

    index: int
    text: str
    style_name: str | None = None
    style_name_id: str | None = None
    outline_level: int | None = None
    is_heading: bool = False
    runs_with_latex: list[str] = field(default_factory=list)
    has_omml: bool = False
    raw: Any = None


@dataclass(slots=True)
class Table:
    """One table, with dimensions and overflow-relevant geometry.

    Attributes:
        index: 0-based position in the document body.
        rows: Number of rows.
        columns: Number of columns, taking the widest row.
        style_name: Table style name, when set.
        column_widths_twips: Declared column widths in twips (1/20 pt), when the
            document declares them. Empty when the layout is automatic.
        usable_width_twips: Page width minus margins, in twips, when known.
        raw: The underlying object.
    """

    index: int
    rows: int
    columns: int
    style_name: str | None = None
    column_widths_twips: list[int] = field(default_factory=list)
    usable_width_twips: int | None = None
    raw: Any = None

    @property
    def declared_width_twips(self) -> int:
        """Total declared column width.

        Returns:
            Sum of ``column_widths_twips``; ``0`` when widths are automatic.
        """
        return sum(self.column_widths_twips)


@dataclass(slots=True)
class RenderResult:
    """Rendered page images plus the intermediate PDF.

    Attributes:
        pdf_path: The intermediate PDF, kept so a human can open it.
        page_images: One PNG per page, in order.
        page_sizes_px: ``(width, height)`` per page image, in pixels.
        dpi: Resolution used for rasterization.
    """

    pdf_path: Path
    page_images: list[Path] = field(default_factory=list)
    page_sizes_px: list[tuple[int, int]] = field(default_factory=list)
    dpi: int = 144


class Document:
    """A loaded document, parsed lazily and rendered on demand.

    Construction is cheap: the file is hashed and its type sniffed, but no
    parser runs until a checker asks for structure or pixels. That keeps
    ``docqc list`` and a text-only run fast, and it means a checker that only
    needs paragraphs never pays for a Word COM round-trip.

    Args:
        path: File to inspect.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        ValueError: If the extension is not supported.
    """

    SUPPORTED = frozenset({".docx", ".doc"})

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise FileNotFoundError(f"document not found: {self.path}")
        self.format = self.path.suffix.lower().lstrip(".")
        if self.path.suffix.lower() not in self.SUPPORTED:
            supported = ", ".join(sorted(self.SUPPORTED))
            raise ValueError(
                f"unsupported format {self.path.suffix!r} for {self.path.name}; "
                f"supported: {supported}"
            )
        self._paragraphs: list[Paragraph] | None = None
        self._tables: list[Table] | None = None
        self._render: RenderResult | None = None
        self._docx: Any = None
        self._styles: StyleTable | None = None

    # -- styles -----------------------------------------------------------

    @property
    def styles(self) -> StyleTable:
        """The document's style definitions, parsed on first access.

        Needed to answer questions the paragraph's own XML cannot: Word cascades
        formatting through the ``basedOn`` chain, so a ``Heading1`` paragraph
        has outline level 0 even though it declares none locally.

        Returns:
            The style table; empty when the document has no ``styles.xml``.
        """
        if self._styles is None:
            self._styles = StyleTable.from_docx(self.path)
        return self._styles

    def resolve_style(self, para: Paragraph) -> ResolvedParagraphStyle:
        """Resolve a paragraph's effective style, inheritance included.

        Direct formatting on the paragraph wins; otherwise the ``basedOn`` chain
        is walked and the first declared value is used — the same precedence
        Word applies when rendering.

        Args:
            para: A paragraph produced by :attr:`paragraphs`.

        Returns:
            The resolved view, including which layer supplied the value so a
            finding can say where the problem actually is.
        """
        table = self.styles
        info = table.get(para.style_name_id)

        resolved = ResolvedParagraphStyle(
            style_id=para.style_name_id,
            style_name=para.style_name,
        )

        # 1. Direct formatting on the paragraph.
        if para.outline_level is not None:
            resolved.outline_level = para.outline_level
            resolved.source = "direct"
            resolved.chain = [s for s in (para.style_name_id,) if s]
            return resolved

        # 2. Walk the style chain.
        if info is not None:
            resolved.chain = [info.style_id]
            current = info
            for _ in range(StyleTable._MAX_CHAIN):  # noqa: SLF001 - shared budget
                if current is None:
                    break
                if current.outline_level is not None:
                    resolved.outline_level = current.outline_level
                    resolved.source = f"style:{current.style_id}"
                    break
                parent_id = current.based_on
                if parent_id is None:
                    break
                parent = table.get(parent_id)
                if parent is None or parent.style_id in resolved.chain:
                    break
                resolved.chain.append(parent.style_id)
                current = parent

        return resolved

    # -- identity ---------------------------------------------------------

    @property
    def size_bytes(self) -> int:
        """File size on disk."""
        return self.path.stat().st_size

    @property
    def sha256(self) -> str:
        """Content hash, computed once and cached.

        Returns:
            Lower-case hex digest.
        """
        cached = getattr(self, "_sha256", None)
        if cached is None:
            digest = hashlib.sha256()
            with self.path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
            cached = digest.hexdigest()
            self._sha256 = cached
        return cached

    # -- structure --------------------------------------------------------

    @property
    def docx(self) -> Any:
        """The underlying ``python-docx`` Document, parsed on first access."""
        if self._docx is None:
            import docx  # imported lazily: keeps startup cheap

            self._docx = docx.Document(str(self.path))
        return self._docx

    def close(self) -> None:
        """Release the underlying package handle.

        ``python-docx`` opens the ``.docx`` as a zip and keeps it open for the
        lifetime of the ``Document``. On Windows that handle blocks deletion, so
        a web job whose upload is deleted while its report still references the
        document fails with ``WinError 32``. Dropping the reference is enough —
        ``zipfile.ZipFile`` closes on collection — and doing it deterministically
        here is what makes "delete the upload after checking it" reliable.

        Safe to call more than once, and safe when nothing was parsed.
        """
        self._docx = None
        self._paragraphs = None
        self._tables = None

    def __enter__(self) -> Document:
        """Context-manager entry.

        Returns:
            This document.
        """
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Release the handle on context exit.

        Args:
            *exc_info: Unused exception triple.
        """
        self.close()

    @property
    def paragraphs(self) -> list[Paragraph]:
        """Every body paragraph, in document order."""
        if self._paragraphs is None:
            self._paragraphs = self._load_paragraphs()
        return self._paragraphs

    @property
    def tables(self) -> list[Table]:
        """Every body table, in document order."""
        if self._tables is None:
            self._tables = self._load_tables()
        return self._tables

    def _load_paragraphs(self) -> list[Paragraph]:
        """Flatten ``python-docx`` paragraphs into :class:`Paragraph` records.

        Both the localized style name and the raw ``w:pStyle`` id are captured:
        the name is what a finding should show the user, the id is what
        ``styles.xml`` is keyed by. Reading only the name would make inheritance
        unresolvable on a localized document.

        Returns:
            One record per body paragraph, index-aligned with the source.
        """
        from .ooxml import LATEX_HINT, has_omml, style_id

        out: list[Paragraph] = []
        for index, para in enumerate(self.docx.paragraphs):
            style = getattr(para.style, "name", None)
            outline = self._outline_level(para)
            latex = [
                run.text
                for run in para.runs
                if run.text and LATEX_HINT.search(run.text)
            ]
            out.append(
                Paragraph(
                    index=index,
                    text=para.text or "",
                    style_name=style,
                    style_name_id=style_id(para),
                    outline_level=outline,
                    is_heading=outline is not None,
                    runs_with_latex=latex,
                    has_omml=has_omml(para),
                    raw=para,
                )
            )
        return out

    @staticmethod
    def _outline_level(para: Any) -> int | None:
        """Read ``w:outlineLvl`` off a paragraph, if present.

        Args:
            para: A ``python-docx`` paragraph.

        Returns:
            0-based outline level, or ``None`` when unset or unparseable.
        """
        try:
            ppr = para._p.pPr  # noqa: SLF001 - python-docx exposes no public path
            if ppr is None or ppr.outlineLvl is None:
                return None
            return int(ppr.outlineLvl.val)
        except (AttributeError, TypeError, ValueError):
            return None

    def _load_tables(self) -> list[Table]:
        """Flatten ``python-docx`` tables into :class:`Table` records.

        Returns:
            One record per body table.
        """
        usable = self._usable_width_twips()
        out: list[Table] = []
        for index, table in enumerate(self.docx.tables):
            widths = self._column_widths(table)
            out.append(
                Table(
                    index=index,
                    rows=len(table.rows),
                    columns=max((len(row.cells) for row in table.rows), default=0),
                    style_name=getattr(table.style, "name", None),
                    column_widths_twips=widths,
                    usable_width_twips=usable,
                    raw=table,
                )
            )
        return out

    @staticmethod
    def _column_widths(table: Any) -> list[int]:
        """Read declared column widths in twips.

        Args:
            table: A ``python-docx`` table.

        Returns:
            One width per grid column; empty when the layout is automatic.
        """
        try:
            grid = table._tbl.tblGrid  # noqa: SLF001 - no public API for tblGrid
            if grid is None:
                return []
            widths = []
            for col in grid.findall(
                "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}gridCol"
            ):
                raw = col.get(
                    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}w"
                )
                if raw is not None:
                    widths.append(int(raw))
            return widths
        except (AttributeError, TypeError, ValueError):
            return []

    def _usable_width_twips(self) -> int | None:
        """Page width minus left/right margins, in twips.

        Returns:
            Usable text width, or ``None`` when the section reports nothing
            usable (which makes overflow claims unverifiable, so checkers must
            treat ``None`` as "unknown" rather than "zero").
        """
        try:
            section = self.docx.sections[0]
            usable = (
                section.page_width.twips
                - section.left_margin.twips
                - section.right_margin.twips
            )
            return int(usable)
        except (AttributeError, IndexError, TypeError, ValueError):
            return None

    # -- rendering --------------------------------------------------------

    def render(self, *, dpi: int = 144, force: bool = False) -> RenderResult:
        """Convert to PDF and rasterize every page.

        Expensive and platform-dependent, so it runs only when a checker
        explicitly asks. The result is cached per instance.

        Args:
            dpi: Rasterization resolution.
            force: Re-render even if a cached result exists.

        Returns:
            The rendered pages.

        Raises:
            RuntimeError: If no rendering backend is available on this machine.
        """
        if self._render is not None and not force and self._render.dpi == dpi:
            return self._render
        from .render import render_document

        self._render = render_document(self.path, dpi=dpi)
        return self._render

    # -- reporting --------------------------------------------------------

    def info(self) -> DocumentInfo:
        """Snapshot the identity and size facts for the report header.

        Returns:
            A :class:`DocumentInfo`; structural counts are included only when
            parsing has already happened or is cheap to trigger.
        """
        paragraph_count: int | None = None
        table_count: int | None = None
        try:
            paragraph_count = len(self.paragraphs)
            table_count = len(self.tables)
        except Exception:  # noqa: BLE001 - identity must survive a broken file
            pass
        return DocumentInfo(
            path=str(self.path),
            name=self.path.name,
            format=self.format,
            size_bytes=self.size_bytes,
            sha256=self.sha256,
            paragraph_count=paragraph_count,
            table_count=table_count,
        )
