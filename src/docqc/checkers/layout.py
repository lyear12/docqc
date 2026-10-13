"""Layout check: does the printed page look right?

Everything here needs pixels, so this is the one checker that pays for a full
render (Word COM -> PDF -> PNG). It exists because the alternative is what the
user was doing by hand: export the PDF, rasterize every page, open them one at a
time, and look for problems. That loop ran hundreds of times.

What pixels can answer that XML cannot:

* **Blank pages.** A stray page break produces a page with no ink. Obvious to a
  human flipping through, invisible in the document tree.
* **Content past the margin.** Word happily lays out text that overflows the
  text column; the reader sees it clipped or spilling into the margin.
* **Table overflow.** A table wider than the text column silently runs off the
  page edge.
* **Near-empty pages.** A page with a few words at the top usually means a
  manual page break landed in the wrong place.

Thresholds are conservative on purpose. A false "content overflows the margin"
sends the author hunting for a problem that does not exist, which costs more
trust than a missed borderline case.

Deliberately *not* checked: whether figures are *appropriate*, or whether text
overlaps a figure. Those need semantic judgement or per-element boxes, which the
rasterized page alone cannot supply.
"""

from __future__ import annotations

from pathlib import Path

from ..document import Document
from ..models import CheckResult, Finding, Location, Severity, Status
from ..registry import register

CHECKER_NAME = "layout"

#: Fraction of a page's pixels that must be non-background for the page to count
#: as having content. Calibrated against a rendered thesis page: body text with
#: normal margins lands around 3-8%, so 0.05% is comfortably below any real page
#: while still catching a genuinely blank one.
BLANK_INK_RATIO = 0.0005

#: Below this ink ratio a page is "nearly empty" — typically a few stray words.
SPARSE_INK_RATIO = 0.004

#: A pixel counts as ink when it is darker than this (0-255). Set high enough to
#: ignore anti-aliasing halos and light table borders.
INK_THRESHOLD = 200

#: Margin band, as a fraction of page width/height, that is expected to stay
#: clear. Word's default 2.54 cm margin on A4 is ~9% of the width; a slightly
#: narrower band avoids flagging legitimate headers and footers.
MARGIN_BAND = 0.03

#: Fraction of ink allowed inside the margin band before it counts as overflow.
#: Headers/footers/page numbers legitimately put a little ink there.
MARGIN_INK_TOLERANCE = 0.0015


@register(
    CHECKER_NAME,
    "空白页、内容溢出页边、表格越界、页面近空",
    order=10,
)
class LayoutChecker:
    """Checks rendered pages for layout defects.

    Skips itself — with a reason — when no rendering backend is available, so a
    machine without Word can still run the XML-level checks.
    """

    name = CHECKER_NAME
    description = "空白页、内容溢出页边、表格越界、页面近空"

    def run(self, document: Document) -> CheckResult:
        """Render the document and inspect every page.

        Args:
            document: The parsed input.

        Returns:
            Findings about blank, sparse, and overflowing pages. When rendering
            is impossible the result is ``SKIPPED`` with the reason in
            ``findings`` as an INFO entry, never an exception.
        """
        from ..render import RenderUnavailableError, detect_backends

        try:
            rendered = document.render()
        except RenderUnavailableError as exc:
            backends = detect_backends()
            return CheckResult(
                checker=self.name,
                status=Status.SKIPPED,
                findings=[
                    Finding(
                        checker=self.name,
                        code="RENDER_UNAVAILABLE",
                        severity=Severity.INFO,
                        message="无法渲染页面，已跳过版面检查。",
                        evidence={
                            "reason": str(exc),
                            "backends": [
                                {"name": b.name, "available": b.available, "detail": b.detail}
                                for b in backends
                            ],
                        },
                        hint="安装 Word 或 LibreOffice，并确保 PyMuPDF 或 poppler 可用。",
                    )
                ],
            )
        except Exception as exc:  # noqa: BLE001 - a render failure is reportable
            return CheckResult(
                checker=self.name,
                status=Status.ERROR,
                findings=[
                    Finding(
                        checker=self.name,
                        code="RENDER_FAILED",
                        severity=Severity.ERROR,
                        message=f"渲染失败：{exc}",
                        evidence={"exception": type(exc).__name__},
                    )
                ],
                error=str(exc),
            )

        findings: list[Finding] = []
        try:
            findings.extend(self._check_pages(rendered))
        except ImportError as exc:
            return CheckResult(
                checker=self.name,
                status=Status.SKIPPED,
                findings=[
                    Finding(
                        checker=self.name,
                        code="IMAGE_LIBS_MISSING",
                        severity=Severity.INFO,
                        message=f"缺少图像分析库，已跳过像素级检查：{exc}",
                        hint="pip install Pillow numpy",
                    )
                ],
            )

        if not findings:
            findings.append(
                Finding(
                    checker=self.name,
                    code="LAYOUT_OK",
                    severity=Severity.INFO,
                    message="版面检查未发现问题。",
                    evidence={
                        "pages": len(rendered.page_images),
                        "dpi": rendered.dpi,
                        "pdf": str(rendered.pdf_path),
                    },
                )
            )

        return CheckResult(checker=self.name, status=_status(findings), findings=findings)

    # -- per page ---------------------------------------------------------

    def _check_pages(self, rendered: object) -> list[Finding]:
        """Inspect every rendered page.

        Args:
            rendered: A :class:`~docqc.document.RenderResult`.

        Returns:
            Findings across all pages.
        """
        import numpy as np
        from PIL import Image

        page_images: list[Path] = rendered.page_images  # type: ignore[attr-defined]
        findings: list[Finding] = []

        for number, image_path in enumerate(page_images, start=1):
            with Image.open(image_path) as handle:
                # Grayscale is all the ink test needs, and it is ~3x faster than
                # analysing three channels.
                grey = np.asarray(handle.convert("L"), dtype=np.uint8)

            total = grey.size
            if total == 0:
                continue
            ink = grey < INK_THRESHOLD
            ink_ratio = float(ink.sum()) / total

            if ink_ratio < BLANK_INK_RATIO:
                findings.append(
                    Finding(
                        checker=self.name,
                        code="BLANK_PAGE",
                        severity=Severity.ERROR,
                        message=f"第 {number} 页几乎全白，没有内容。",
                        location=Location(page=number),
                        evidence={
                            "ink_ratio": round(ink_ratio, 6),
                            "threshold": BLANK_INK_RATIO,
                            "image": image_path.name,
                        },
                        hint="检查是否有孤立的分页符或空段落。",
                    )
                )
                continue

            if ink_ratio < SPARSE_INK_RATIO:
                findings.append(
                    Finding(
                        checker=self.name,
                        code="SPARSE_PAGE",
                        severity=Severity.WARNING,
                        message=f"第 {number} 页内容很少（墨迹占比 {ink_ratio:.2%}），"
                        "可能是分页位置不当。",
                        location=Location(page=number),
                        evidence={
                            "ink_ratio": round(ink_ratio, 6),
                            "threshold": SPARSE_INK_RATIO,
                            "image": image_path.name,
                        },
                        hint="确认该页是否为章节首页或图表页；否则调整分页。",
                    )
                )

            overflow = self._check_margins(ink, number, image_path)
            if overflow is not None:
                findings.append(overflow)

        return findings

    def _check_margins(self, ink: object, page: int, image_path: Path) -> Finding | None:
        """Look for ink inside the margin band.

        The band is the outer ``MARGIN_BAND`` fraction on each side. A little ink
        is normal (headers, footers, page numbers), so the test compares against
        ``MARGIN_INK_TOLERANCE`` rather than demanding zero.

        Args:
            ink: Boolean array, True where a pixel is dark.
            page: 1-based page number.
            image_path: Source image, for the evidence trail.

        Returns:
            A finding when the band holds more ink than tolerated, else ``None``.
        """
        import numpy as np

        array = np.asarray(ink)
        height, width = array.shape
        band_y = max(1, int(height * MARGIN_BAND))
        band_x = max(1, int(width * MARGIN_BAND))

        edges = {
            "top": array[:band_y, :],
            "bottom": array[height - band_y :, :],
            "left": array[:, :band_x],
            "right": array[:, width - band_x :],
        }

        worst_name = ""
        worst_ratio = 0.0
        for name, region in edges.items():
            if region.size == 0:
                continue
            ratio = float(region.sum()) / region.size
            if ratio > worst_ratio:
                worst_name, worst_ratio = name, ratio

        if worst_ratio <= MARGIN_INK_TOLERANCE:
            return None

        return Finding(
            checker=self.name,
            code="CONTENT_IN_MARGIN",
            severity=Severity.WARNING,
            message=f"第 {page} 页的{_side_label(worst_name)}页边区域有内容，"
            "可能溢出正文范围。",
            location=Location(page=page),
            evidence={
                "side": worst_name,
                "ink_ratio_in_band": round(worst_ratio, 6),
                "tolerance": MARGIN_INK_TOLERANCE,
                "band_fraction": MARGIN_BAND,
                "image": image_path.name,
            },
            hint="检查是否有超宽表格、未换行的长公式或过大的图片。",
        )


def _side_label(side: str) -> str:
    """Translate a margin name for the message.

    Args:
        side: One of ``top``/``bottom``/``left``/``right``.

    Returns:
        A short Chinese label.
    """
    return {"top": "上", "bottom": "下", "left": "左", "right": "右"}.get(side, side)


def _status(findings: list[Finding]) -> Status:
    """Derive a checker status from its findings.

    Args:
        findings: Everything the checker found.

    Returns:
        ``FAIL`` if any ERROR, ``WARN`` if any WARNING, else ``PASS``.
    """
    severities = {f.severity for f in findings}
    if Severity.ERROR in severities:
        return Status.FAIL
    if Severity.WARNING in severities:
        return Status.WARN
    return Status.PASS


__all__ = ["LayoutChecker"]
