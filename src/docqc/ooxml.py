"""Raw OOXML helpers that ``python-docx`` does not expose.

``python-docx`` models the parts of WordprocessingML that most documents use.
Quality checking needs a few things it deliberately leaves alone:

* **Is this paragraph a real equation?** Word stores equations as OMML
  (``m:oMath``). Text that merely *looks* like math — ``$x^2$``, ``\\frac{a}{b}``
  — is plain text and will print literally. Telling those apart is the whole
  point of the formula checker.
* **What is the paragraph's outline level?** Headings carry ``w:outlineLvl``
  inside ``w:pPr``. Without it a "heading" is just bold text: the table of
  contents will not see it and numbering will not follow it.
* **Which package parts exist?** A ``.docx`` is a zip; some defects (missing
  styles part, dangling relationship) are only visible at that level.

Everything here is read-only and namespace-explicit. Element tags are written as
``{namespace}localName`` rather than relying on prefix declarations, because
prefixes are arbitrary in OOXML and a document is free to call the math
namespace ``m:`` or ``mml:``.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any

# -- namespaces ---------------------------------------------------------------

NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"

#: Qualified tag for an OMML inline equation.
TAG_OMML = f"{{{NS_M}}}oMath"
#: Qualified tag for a display equation block (the paragraph-level wrapper).
TAG_OMML_PARA = f"{{{NS_M}}}oMathPara"
#: Qualified tag for a paragraph's property bag.
TAG_PPR = f"{{{NS_W}}}pPr"
#: Qualified tag for a run's property bag.
TAG_RPR = f"{{{NS_W}}}rPr"
#: Qualified tag for a table's property bag.
TAG_TBLPR = f"{{{NS_W}}}tblPr"
#: Qualified tag for the table grid (column definitions).
TAG_TBLGRID = f"{{{NS_W}}}tblGrid"
#: Qualified tag for one grid column.
TAG_GRIDCOL = f"{{{NS_W}}}gridCol"

# -- LaTeX detection ----------------------------------------------------------

#: Text that is almost certainly unrendered LaTeX rather than prose.
#:
#: Deliberately narrow. A false positive here tells the author to "fix" a
#: paragraph that was fine, which erodes trust in the tool faster than a missed
#: detection does. Each alternative targets a construct that has no reason to
#: appear in finished prose:
#:
#:   \cmd{...}      a LaTeX macro with a braced argument
#:   \cmd           a bare known macro name
#:   $...$          inline math delimiters, at least one character inside
#:   \begin{env}    an environment opener
#:
#: The known-macro list is intentionally short and high-signal. Expanding it is
#: cheap; the risk is only in adding names that appear in ordinary Chinese or
#: English text.
_KNOWN_MACROS = (
    r"frac|dfrac|tfrac|sqrt|sum|prod|int|iint|oint|lim|log|ln|exp|sin|cos|tan|"
    r"alpha|beta|gamma|delta|epsilon|theta|lambda|mu|sigma|omega|pi|"
    r"times|cdot|leq|geq|neq|approx|equiv|infty|partial|nabla|"
    r"begin|end|left|right|text|mathrm|mathbf|mathcal|overline|underline|"
    r"hat|bar|vec|dot|tilde|quad|qquad|hline|cline|multicolumn|"
    r"ref|label|cite|includegraphics|caption|centering|item|textbf|textit|emph"
)

LATEX_HINT = re.compile(
    r"(?:"
    r"\\[A-Za-z]+\s*\{[^{}]*\}"  # \frac{a}{b}
    r"|\\(?:" + _KNOWN_MACROS + r")\b"  # \alpha, \begin
    r"|\$[^$\n]{1,200}\$"  # $x^2$
    r")"
)

#: OOXML paragraph-property element order.
#:
#: Word validates ``w:pPr`` children against this sequence and rejects a
#: document whose children are out of order — a defect that is invisible in
#: ``python-docx`` but fatal in Word. Kept here so the structure checker can
#: report a precise "expected X before Y" message.
PPR_ORDER = (
    "pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr",
    "widowControl", "numPr", "suppressLineNumbers", "pBdr", "shd", "tabs",
    "suppressAutoHyphens", "kinsoku", "wordWrap", "overflowPunct",
    "topLinePunct", "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd",
    "snapToGrid", "spacing", "ind", "contextualSpacing", "mirrorIndents",
    "suppressOverlap", "jc", "textDirection", "textAlignment", "textboxTightWrap",
    "outlineLvl", "divId", "cnfStyle", "rPr", "sectPr", "pPrChange",
)


def has_omml(paragraph: Any) -> bool:
    """Whether a paragraph contains a real Word equation.

    Word represents equations with OMML (``m:oMath`` / ``m:oMathPara``), which
    ``python-docx`` does not model. Detection walks the paragraph's XML instead.

    Args:
        paragraph: A ``python-docx`` ``Paragraph``.

    Returns:
        True when at least one OMML element is present.
    """
    try:
        xml = paragraph._p  # noqa: SLF001 - the only route to the raw element
    except AttributeError:
        return False
    return xml.find(f".//{TAG_OMML}") is not None or xml.find(f".//{TAG_OMML_PARA}") is not None


def outline_level(paragraph: Any) -> int | None:
    """Read the paragraph's outline level.

    Args:
        paragraph: A ``python-docx`` ``Paragraph``.

    Returns:
        The 0-based level from ``w:outlineLvl``, or ``None`` when absent or not
        an integer.
    """
    try:
        ppr = paragraph._p.pPr  # noqa: SLF001
    except AttributeError:
        return None
    if ppr is None:
        return None
    element = ppr.find(f"{{{NS_W}}}outlineLvl")
    if element is None:
        return None
    raw = element.get(f"{{{NS_W}}}val")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def style_id(paragraph: Any) -> str | None:
    """Read the paragraph's ``w:pStyle`` value.

    Args:
        paragraph: A ``python-docx`` ``Paragraph``.

    Returns:
        The style id (e.g. ``Heading1``), or ``None`` when the paragraph has no
        explicit style — which is itself a defect for anything meant to be a
        heading.
    """
    try:
        ppr = paragraph._p.pPr  # noqa: SLF001
    except AttributeError:
        return None
    if ppr is None:
        return None
    element = ppr.find(f"{{{NS_W}}}pStyle")
    if element is None:
        return None
    raw = element.get(f"{{{NS_W}}}val")
    return None if raw is None else str(raw)


def ppr_child_order(paragraph: Any) -> list[str]:
    """List a paragraph's ``w:pPr`` children in document order.

    Args:
        paragraph: A ``python-docx`` ``Paragraph``.

    Returns:
        Local names (no namespace), in the order they appear; empty when the
        paragraph has no properties.
    """
    try:
        ppr = paragraph._p.pPr  # noqa: SLF001
    except AttributeError:
        return []
    if ppr is None:
        return []
    names: list[str] = []
    for child in ppr:
        tag = child.tag
        if not isinstance(tag, str):
            continue
        names.append(tag.rsplit("}", 1)[-1])
    return names


def ppr_order_violations(paragraph: Any) -> list[tuple[str, str]]:
    """Find ``w:pPr`` children that violate the schema sequence.

    Word enforces this order and refuses to open a document that breaks it, so a
    violation is ship-blocking even though the file parses fine in Python.

    Args:
        paragraph: A ``python-docx`` ``Paragraph``.

    Returns:
        ``(earlier, later)`` pairs where ``earlier`` must precede ``later`` but
        does not. Empty when the order is valid or unknown.
    """
    names = ppr_child_order(paragraph)
    if len(names) < 2:
        return []
    rank = {name: i for i, name in enumerate(PPR_ORDER)}
    known = [name for name in names if name in rank]
    violations: list[tuple[str, str]] = []
    for earlier, later in zip(known, known[1:], strict=False):
        if rank[earlier] > rank[later]:
            violations.append((earlier, later))
    return violations


def package_parts(path: Path) -> list[str]:
    """List the internal parts of an Office file.

    Args:
        path: A ``.docx``/``.docm`` file.

    Returns:
        Part names, e.g. ``word/document.xml``; empty when the file is not a
        readable zip (a corrupt container is reported by the caller).
    """
    try:
        with zipfile.ZipFile(path) as archive:
            return sorted(archive.namelist())
    except (zipfile.BadZipFile, OSError):
        return []


def read_part(path: Path, part: str) -> bytes | None:
    """Read one internal part.

    Args:
        path: The Office file.
        part: Part name as returned by :func:`package_parts`.

    Returns:
        The part's bytes, or ``None`` when the archive or part is unreadable.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            return archive.read(part)
    except (zipfile.BadZipFile, KeyError, OSError):
        return None


def content_types(path: Path) -> set[str]:
    """Collect the content types declared by the package.

    Args:
        path: The Office file.

    Returns:
        Declared ``ContentType`` values; empty when unreadable.
    """
    raw = read_part(path, "[Content_Types].xml")
    if raw is None:
        return set()
    return set(re.findall(rb'ContentType="([^"]+)"', raw))


__all__ = [
    "LATEX_HINT",
    "NS_CT",
    "NS_M",
    "NS_R",
    "NS_W",
    "PPR_ORDER",
    "TAG_GRIDCOL",
    "TAG_OMML",
    "TAG_OMML_PARA",
    "TAG_PPR",
    "TAG_RPR",
    "TAG_TBLGRID",
    "TAG_TBLPR",
    "content_types",
    "has_omml",
    "outline_level",
    "package_parts",
    "ppr_child_order",
    "ppr_order_violations",
    "read_part",
    "style_id",
]
