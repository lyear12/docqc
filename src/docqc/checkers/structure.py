"""Structure check: is the document's skeleton intact?

Checks the things a table of contents, cross-reference, or numbering pass needs
in order to work at all:

* **Heading styles.** A "heading" that is merely bold text carries no
  ``w:pStyle``, so Word cannot build a TOC from it and numbering skips it. This
  is the single most common defect in hand-assembled theses.
* **Outline levels.** ``w:outlineLvl`` is what the navigation pane and the TOC
  read; a heading style without it is invisible to both.
* **Heading depth jumps.** Going from level 1 straight to level 3 means a
  document whose outline has a hole — usually a sign a section was deleted or
  pasted from elsewhere.
* **``w:pPr`` child order.** Word validates this sequence and refuses to open a
  file that breaks it. Python parses such a file happily, so the defect is
  invisible until someone double-clicks it.
* **Package integrity.** A ``.docx`` is a zip; a missing ``styles.xml`` or
  ``document.xml`` means the file will not open at all.

Deliberately *not* checked here: figure/table numbering and cross-reference
targets. Those need the render layer to resolve page numbers, so they belong to
a later checker rather than being guessed at from the XML.
"""

from __future__ import annotations

from ..document import Document, Paragraph
from ..models import CheckResult, Finding, Location, Severity, Status
from ..ooxml import package_parts, ppr_order_violations
from ..registry import register

CHECKER_NAME = "structure"

#: Paragraphs whose style name looks like a heading.
#:
#: Matched against the *style name* rather than the style id because Word
#: localizes names ("标题 1" in Chinese) while ids stay English (``Heading1``).
#: Both spellings are therefore accepted, plus the ``heading N`` form that some
#: templates use.
_HEADING_MARKERS = ("heading", "标题", "标题 ", "heading ")

#: Style names that legitimately contain a heading-like word without being one.
_HEADING_FALSE_FRIENDS = ("toc", "目录", "caption", "题注")


def _looks_like_heading(style_name: str | None) -> bool:
    """Whether a style name denotes a heading.

    Args:
        style_name: ``Paragraph.style_name``.

    Returns:
        True when the name matches a known heading pattern and is not one of the
        known false friends.
    """
    if not style_name:
        return False
    lowered = style_name.strip().lower()
    if any(friend in lowered for friend in _HEADING_FALSE_FRIENDS):
        return False
    if lowered.startswith("heading"):
        return True
    return any(marker in style_name for marker in _HEADING_MARKERS)


def _heading_level(paragraph_style: str | None, outline: int | None) -> int | None:
    """Resolve a heading's level from its outline level or style name.

    The outline level wins because it is what Word actually uses. The style name
    is only a fallback for a heading style whose chain declares no level at all
    — in which case the name is the only signal available.

    Args:
        paragraph_style: Style name or id, when present.
        outline: The *inherited* outline level, when known.

    Returns:
        1-based level, or ``None`` when the paragraph is not a heading.
    """
    if outline is not None:
        return outline + 1
    if paragraph_style:
        digits = "".join(ch for ch in paragraph_style if ch.isdigit())
        if digits:
            return int(digits)
        if paragraph_style.strip().lower().startswith("heading"):
            return 1
    return None


@register(
    CHECKER_NAME,
    "标题样式、大纲层级、pPr 顺序与包完整性",
    order=30,
)
class StructureChecker:
    """Checks the document skeleton.

    Runs entirely on the parsed XML layer, so it is fast and needs no renderer.
    """

    name = CHECKER_NAME
    description = "标题样式、大纲层级、pPr 顺序与包完整性"

    def run(self, document: Document) -> CheckResult:
        """Inspect the document's structure.

        Args:
            document: The parsed input.

        Returns:
            Findings covering headings, outline levels, property order, and
            package integrity. A file that cannot be parsed at all yields a
            single ERROR rather than a stack trace.
        """
        findings: list[Finding] = []

        # Package integrity is checked first and independently of parsing. A
        # corrupt container makes `python-docx` raise, and if that were treated
        # as fatal the user would see a generic "cannot parse" instead of the
        # specific "this is not a zip" that says what to do about it.
        package_findings = self._check_package(document)
        findings.extend(package_findings)
        if any(f.code == "NOT_A_PACKAGE" for f in package_findings):
            return CheckResult(
                checker=self.name, status=Status.FAIL, findings=findings
            )

        try:
            paragraphs = document.paragraphs
        except Exception as exc:  # noqa: BLE001 - an unreadable file is a finding
            findings.append(
                Finding(
                    checker=self.name,
                    code="UNREADABLE_DOCUMENT",
                    severity=Severity.ERROR,
                    message=f"无法解析文档内容：{exc}",
                    evidence={"exception": type(exc).__name__},
                    hint="确认文件是有效的 .docx（不是改了扩展名的 .doc 或加密文件）。",
                )
            )
            return CheckResult(
                checker=self.name, status=Status.FAIL, findings=findings
            )

        findings.extend(self._check_headings(document, paragraphs))
        findings.extend(self._check_fake_headings(paragraphs))
        findings.extend(self._check_property_order(paragraphs))

        if not findings:
            findings.append(
                Finding(
                    checker=self.name,
                    code="STRUCTURE_OK",
                    severity=Severity.INFO,
                    message="结构检查未发现问题。",
                    evidence={
                        "paragraphs": len(paragraphs),
                        "tables": len(document.tables),
                    },
                )
            )

        return CheckResult(checker=self.name, status=_status(findings), findings=findings)

    # -- package ----------------------------------------------------------

    def _check_package(self, document: Document) -> list[Finding]:
        """Verify the OOXML package contains the parts Word requires.

        Args:
            document: The parsed input.

        Returns:
            Findings for a corrupt container or missing required parts.
        """
        parts = package_parts(document.path)
        if not parts:
            return [
                Finding(
                    checker=self.name,
                    code="NOT_A_PACKAGE",
                    severity=Severity.ERROR,
                    message="文件不是有效的 OOXML 包（zip 容器无法读取）。",
                    evidence={"path": document.path.name},
                    hint="用 Word 打开并「另存为」一次，或从原始来源重新导出。",
                )
            ]

        required = {"word/document.xml": "正文"}
        missing = [name for name in required if name not in parts]
        findings: list[Finding] = []
        for name in missing:
            findings.append(
                Finding(
                    checker=self.name,
                    code="MISSING_REQUIRED_PART",
                    severity=Severity.ERROR,
                    message=f"包内缺少必需部件 {name}（{required[name]}）。",
                    evidence={"part": name, "part_count": len(parts)},
                    hint="文件很可能已损坏，需要重新导出。",
                )
            )

        if "word/styles.xml" not in parts:
            findings.append(
                Finding(
                    checker=self.name,
                    code="MISSING_STYLES_PART",
                    severity=Severity.WARNING,
                    message="包内没有 styles.xml，所有段落都只能是直接格式，无法生成目录。",
                    evidence={"part_count": len(parts)},
                    hint="用带样式模板的 Word 文档重建，或补上样式定义。",
                )
            )
        return findings

    # -- headings ---------------------------------------------------------

    def _check_headings(
        self, document: Document, paragraphs: list[Paragraph]
    ) -> list[Finding]:
        """Check heading styles, outline levels, and depth continuity.

        Outline level is read *after inheritance*, not from the paragraph alone.
        Word cascades ``w:outlineLvl`` from the style chain, so a ``Heading1``
        paragraph that declares nothing locally is still at level 0. Checking
        the raw paragraph value produced 40 false warnings on a healthy thesis —
        see :mod:`docqc.styles` for the resolution rules.

        Args:
            document: The parsed input, used to resolve style inheritance.
            paragraphs: Flattened paragraphs.

        Returns:
            Findings about headings.
        """
        findings: list[Finding] = []
        headings: list[tuple[int, int, str]] = []  # (paragraph index, level, text)

        for para in paragraphs:
            resolved = document.resolve_style(para)
            style = para.style_name or para.style_name_id
            is_heading_style = _looks_like_heading(para.style_name) or _looks_like_heading(
                para.style_name_id
            )
            level = _heading_level(style, resolved.outline_level)

            # A heading style whose effective outline level is still unknown
            # means neither the paragraph nor any style in its chain declares
            # one: that genuinely breaks the TOC, so it is worth reporting.
            if is_heading_style and resolved.outline_level is None:
                findings.append(
                    Finding(
                        checker=self.name,
                        code="HEADING_WITHOUT_OUTLINE_LEVEL",
                        severity=Severity.WARNING,
                        message=f"段落使用标题样式「{style}」但整个样式链都没有大纲级别，"
                        "目录和导航窗格都看不到它。",
                        location=Location(
                            paragraph=para.index,
                            text=para.text[:80] or None,
                        ),
                        evidence={
                            "style": style,
                            "style_id": para.style_name_id,
                            "chain": resolved.chain,
                            "outline_level": None,
                        },
                        hint="在该标题样式的定义里设置大纲级别（开始 → 样式 → 修改）。",
                    )
                )

            # Direct formatting that overrides a heading style is suspicious:
            # it means the document has two sources of truth for the same level.
            if (
                is_heading_style
                and resolved.source == "direct"
                and resolved.outline_level is not None
            ):
                inherited = document.styles.resolve(para.style_name_id, "outline_level")
                if inherited is not None and inherited != resolved.outline_level:
                    findings.append(
                        Finding(
                            checker=self.name,
                            code="OUTLINE_LEVEL_OVERRIDES_STYLE",
                            severity=Severity.WARNING,
                            message=(
                                f"段落直接设定的大纲级别（{resolved.outline_level}）"
                                f"与样式「{style}」的定义（{inherited}）不一致。"
                            ),
                            location=Location(
                                paragraph=para.index, text=para.text[:80] or None
                            ),
                            evidence={
                                "direct_outline_level": resolved.outline_level,
                                "style_outline_level": inherited,
                                "style_id": para.style_name_id,
                            },
                            hint="删掉段落的直接大纲级别，让它跟随样式；否则目录层级会错乱。",
                        )
                    )

            if (
                level is not None
                and not is_heading_style
                and resolved.outline_level is not None
                and resolved.source == "direct"
            ):
                findings.append(
                    Finding(
                        checker=self.name,
                        code="OUTLINE_LEVEL_WITHOUT_HEADING_STYLE",
                        severity=Severity.WARNING,
                        message=f"段落有大纲级别 {level} 却没有标题样式，"
                        "目录条目会缺少样式和编号。",
                        location=Location(
                            paragraph=para.index,
                            text=para.text[:80] or None,
                        ),
                        evidence={
                            "style": style,
                            "outline_level": resolved.outline_level,
                            "source": resolved.source,
                        },
                        hint="改用对应的「标题 N」样式，而不是手动设大纲级别。",
                    )
                )

            if level is not None:
                headings.append((para.index, level, para.text[:60]))

        findings.extend(self._check_depth_jumps(headings))

        if not headings:
            findings.append(
                Finding(
                    checker=self.name,
                    code="NO_HEADINGS",
                    severity=Severity.WARNING,
                    message="全文没有任何带大纲级别的标题，无法生成目录。",
                    evidence={"paragraphs": len(paragraphs)},
                    hint="用「标题 1 / 标题 2」等样式标记章节。",
                )
            )
        return findings

    def _check_depth_jumps(self, headings: list[tuple[int, int, str]]) -> list[Finding]:
        """Find places where the heading depth skips a level.

        Args:
            headings: ``(paragraph index, level, text)`` in document order.

        Returns:
            One finding per jump deeper than one level.
        """
        findings: list[Finding] = []
        for (_, previous, previous_text), (index, current, text) in zip(
            headings, headings[1:], strict=False
        ):
            if current - previous > 1:
                findings.append(
                    Finding(
                        checker=self.name,
                        code="HEADING_LEVEL_JUMP",
                        severity=Severity.WARNING,
                        message=f"标题层级从 {previous} 跳到 {current}，中间缺了一级。",
                        location=Location(paragraph=index, text=text or None),
                        evidence={
                            "previous_level": previous,
                            "current_level": current,
                            "previous_text": previous_text,
                        },
                        hint="补齐中间层级，或把该标题降为上一级的下属。",
                    )
                )
        return findings

    # -- fake headings ----------------------------------------------------

    def _check_fake_headings(self, paragraphs: list[Paragraph]) -> list[Finding]:
        """Find "headings" that are only formatted text.

        A paragraph that is short, bold, larger than body text, and styled
        ``Normal`` is almost always someone who liked the look of a heading but
        never applied one. The reader sees a heading; Word sees body text, so the
        TOC omits it and numbering skips it.

        Heuristics are conservative on purpose — a paragraph must be short *and*
        bold *and* (larger font *or* centred) before it is reported. Prose that
        happens to contain a bold word must not be flagged.

        Args:
            paragraphs: Flattened paragraphs.

        Returns:
            One finding per suspected fake heading.
        """
        findings: list[Finding] = []
        for para in paragraphs:
            text = (para.text or "").strip()
            # Headings are short. Long paragraphs are prose, whatever their runs
            # look like; 60 characters covers the longest real heading seen.
            if not text or len(text) > 60:
                continue
            if para.style_name_id not in (None, "Normal", "BodyText", "正文"):
                continue
            if _looks_like_heading(para.style_name) or _looks_like_heading(para.style_name_id):
                continue

            raw = para.raw
            if raw is None:
                continue
            runs = [r for r in getattr(raw, "runs", []) if (r.text or "").strip()]
            if not runs:
                continue

            all_bold = all(bool(r.bold) for r in runs)
            if not all_bold:
                continue

            sizes = [r.font.size.pt for r in runs if r.font.size is not None]
            enlarged = bool(sizes) and max(sizes) >= 14.0
            centred = _is_centred(raw)

            if not (enlarged or centred):
                continue

            findings.append(
                Finding(
                    checker=self.name,
                    code="SUSPECTED_FAKE_HEADING",
                    severity=Severity.WARNING,
                    message="这一行看起来是标题（加粗"
                    + ("、字号偏大" if enlarged else "")
                    + ("、居中" if centred else "")
                    + "），但没有用标题样式，目录里不会出现。",
                    location=Location(paragraph=para.index, text=text),
                    evidence={
                        "style": para.style_name or "(无样式)",
                        "bold": True,
                        "max_font_pt": max(sizes) if sizes else None,
                        "centred": centred,
                        "length": len(text),
                    },
                    hint="选中这一行，套用「标题 1 / 标题 2」样式。",
                )
            )
        return findings

    # -- property order ---------------------------------------------------

    def _check_property_order(self, paragraphs: list[Paragraph]) -> list[Finding]:
        """Find ``w:pPr`` children in an order Word rejects.

        Args:
            paragraphs: Flattened paragraphs.

        Returns:
            One finding per offending paragraph.
        """
        findings: list[Finding] = []
        for para in paragraphs:
            raw = para.raw
            if raw is None:
                continue
            violations = ppr_order_violations(raw)
            if violations:
                findings.append(
                    Finding(
                        checker=self.name,
                        code="PPR_CHILD_ORDER",
                        severity=Severity.ERROR,
                        message="段落属性（w:pPr）子元素顺序不符合 OOXML 规范，"
                        "Word 可能拒绝打开此文件。",
                        location=Location(
                            paragraph=para.index,
                            text=para.text[:80] or None,
                        ),
                        evidence={
                            "violations": [f"{a} 出现在 {b} 之前" for a, b in violations]
                        },
                        hint="按 schema 顺序重排 w:pPr 子元素；通常由脚本拼装 XML 时引入。",
                    )
                )
        return findings


def _is_centred(paragraph: object) -> bool:
    """Whether a paragraph is centre-aligned.

    Args:
        paragraph: A ``python-docx`` paragraph.

    Returns:
        True for centre alignment; False when unset or any other alignment.
    """
    try:
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        alignment = getattr(paragraph, "alignment", None)
        return bool(alignment == WD_ALIGN_PARAGRAPH.CENTER)
    except (AttributeError, ImportError):
        return False


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


__all__ = ["StructureChecker"]
