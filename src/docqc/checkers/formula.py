"""Formula check: are equations actually equations?

Word stores real equations as OMML (``m:oMath``). Anything else that *looks*
like math — ``$x^2$``, ``\\frac{a}{b}``, ``\\alpha`` — is plain text and prints
literally, so the reader sees backslashes and dollar signs instead of symbols.

This is the defect that motivated the tool: a hand-assembled thesis shipped with
two display formulas still in raw LaTeX, and it was only caught by a human
reading a rendered page.

The check therefore answers three questions:

1. **Is this paragraph meant to be math?** Judged from LaTeX syntax, which is
   unambiguous in finished prose.
2. **Is it actually math?** Answered by looking for OMML in the same paragraph.
3. **Do inline markers pair up?** An odd number of ``$`` means the author opened
   math and never closed it — a defect that renders as a literal dollar sign and
   swallows the rest of the sentence.

Deliberately *not* checked here: whether the math is *correct*. That needs a
CAS and is out of scope; the tool checks presentation, not mathematics.
"""

from __future__ import annotations

import re

from ..document import Document
from ..models import CheckResult, Finding, Location, Severity, Status
from ..registry import register

CHECKER_NAME = "formula"

#: A ``$``-delimited span, used to spot unbalanced delimiters.
_DOLLAR = re.compile(r"(?<!\\)\$")

#: LaTeX constructs that only ever appear in *source*, never in finished prose.
#:
#: Narrower than :data:`docqc.ooxml.LATEX_HINT`: this set is used to decide that
#: a paragraph is *definitely* raw LaTeX rather than a false positive, so it
#: excludes anything ambiguous (a lone backslash, a ``_`` subscript).
_DEFINITE_LATEX = re.compile(
    r"(?:"
    r"\\begin\{(?:equation|align|array|matrix|cases|split|gather)\*?\}"
    r"|\\end\{(?:equation|align|array|matrix|cases|split|gather)\*?\}"
    r"|\\frac\s*\{[^{}]*\}\s*\{[^{}]*\}"
    r"|\\sqrt\s*\{[^{}]*\}"
    r"|\\sum_|\\int_|\\prod_"
    r"|\\(?:alpha|beta|gamma|delta|theta|lambda|mu|sigma|omega|pi|infty)\b"
    r"|\\(?:times|cdot|leq|geq|neq|approx|equiv|partial|nabla)\b"
    r")"
)

#: Equation-numbering pattern, e.g. ``(12)`` or ``（12）`` at end of line.
_EQUATION_NUMBER = re.compile(r"[（(]\s*(\d+)\s*[)）]\s*$")


@register(
    CHECKER_NAME,
    "公式是否真渲染（OMML）、裸 LaTeX、编号连续性",
    order=20,
)
class FormulaChecker:
    """Checks that formulas are rendered and numbered consistently.

    Runs on the XML layer, so it needs no renderer and stays fast.
    """

    name = CHECKER_NAME
    description = "公式是否真渲染（OMML）、裸 LaTeX、编号连续性"

    def run(self, document: Document) -> CheckResult:
        """Inspect every paragraph for formula defects.

        Args:
            document: The parsed input.

        Returns:
            Findings covering raw LaTeX, unbalanced delimiters, and equation
            numbering gaps.
        """
        try:
            paragraphs = document.paragraphs
        except Exception as exc:  # noqa: BLE001
            # The structure checker owns "the container is broken"; repeating
            # that verdict here would double-count one defect as two. This
            # checker stays silent and reports SKIPPED instead, so the summary
            # counts one problem rather than three.
            return CheckResult(
                checker=self.name,
                status=Status.SKIPPED,
                findings=[
                    Finding(
                        checker=self.name,
                        code="SKIPPED_UNREADABLE",
                        severity=Severity.INFO,
                        message="文档无法解析，已跳过公式检查（原因见 structure 检查器）。",
                        evidence={"exception": type(exc).__name__},
                    )
                ],
            )

        findings: list[Finding] = []
        numbered: list[tuple[int, int, str]] = []  # (paragraph, number, text)
        omml_count = 0

        for para in paragraphs:
            if para.has_omml:
                omml_count += 1

            text = para.text or ""
            if not text.strip():
                continue

            findings.extend(self._check_raw_latex(para.index, text, para.has_omml))
            findings.extend(self._check_dollar_balance(para.index, text))

            match = _EQUATION_NUMBER.search(text.rstrip())
            if match:
                numbered.append((para.index, int(match.group(1)), text[:60]))

        findings.extend(self._check_numbering(numbered))

        if not findings:
            findings.append(
                Finding(
                    checker=self.name,
                    code="FORMULA_OK",
                    severity=Severity.INFO,
                    message="公式检查未发现问题。",
                    evidence={
                        "omml_paragraphs": omml_count,
                        "numbered_equations": len(numbered),
                    },
                )
            )

        return CheckResult(checker=self.name, status=_status(findings), findings=findings)

    # -- raw LaTeX --------------------------------------------------------

    def _check_raw_latex(self, index: int, text: str, has_omml: bool) -> list[Finding]:
        """Flag paragraphs containing unrendered LaTeX.

        Args:
            index: Paragraph index.
            text: Paragraph text.
            has_omml: Whether the paragraph already contains a real equation.

        Returns:
            One finding when definite LaTeX source appears in prose.
        """
        if not _DEFINITE_LATEX.search(text):
            return []

        # A paragraph that has OMML *and* LaTeX-looking text is usually an
        # equation plus a caption quoting it; report it but at lower confidence.
        severity = Severity.WARNING if has_omml else Severity.ERROR
        snippet = text[:120] + ("…" if len(text) > 120 else "")
        return [
            Finding(
                checker=self.name,
                code="RAW_LATEX_IN_TEXT",
                severity=severity,
                message=(
                    "段落含未渲染的 LaTeX 源码，读者会看到反斜杠和花括号而不是公式。"
                    if not has_omml
                    else "段落在公式之外还残留 LaTeX 源码。"
                ),
                location=Location(paragraph=index, text=snippet),
                evidence={
                    "matched": _DEFINITE_LATEX.findall(text)[:5],
                    "has_omml": has_omml,
                },
                hint="用 Word 公式编辑器重排，或删除残留的 LaTeX 记号。",
            )
        ]

    # -- delimiters -------------------------------------------------------

    def _check_dollar_balance(self, index: int, text: str) -> list[Finding]:
        """Flag an odd number of unescaped ``$`` delimiters.

        Args:
            index: Paragraph index.
            text: Paragraph text.

        Returns:
            One finding when the delimiters do not pair up.
        """
        marks = _DOLLAR.findall(text)
        if len(marks) % 2 == 0:
            return []
        return [
            Finding(
                checker=self.name,
                code="UNBALANCED_MATH_DELIMITER",
                severity=Severity.ERROR,
                message=f"段落中有 {len(marks)} 个未转义的 $，数量为奇数，"
                "行内公式没有闭合。",
                location=Location(paragraph=index, text=text[:120]),
                evidence={"dollar_count": len(marks)},
                hint="补齐或删除多余的 $；若本意是美元符号请写成 \\$。",
            )
        ]

    # -- numbering --------------------------------------------------------

    def _check_numbering(self, numbered: list[tuple[int, int, str]]) -> list[Finding]:
        """Find gaps or duplicates in equation numbering.

        Args:
            numbered: ``(paragraph index, equation number, text)`` in document
                order.

        Returns:
            Findings for duplicate and skipped numbers.
        """
        findings: list[Finding] = []
        seen: dict[int, int] = {}
        for index, number, text in numbered:
            if number in seen:
                findings.append(
                    Finding(
                        checker=self.name,
                        code="DUPLICATE_EQUATION_NUMBER",
                        severity=Severity.ERROR,
                        message=f"公式编号 ({number}) 重复出现。",
                        location=Location(paragraph=index, text=text or None),
                        evidence={
                            "number": number,
                            "first_at_paragraph": seen[number],
                            "again_at_paragraph": index,
                        },
                        hint="重新编号，或把其中一个改为引用而不带编号。",
                    )
                )
            else:
                seen[number] = index

        if len(numbered) >= 2:
            numbers = [n for _, n, _ in numbered]
            expected = list(range(min(numbers), max(numbers) + 1))
            missing = sorted(set(expected) - set(numbers))
            if missing:
                findings.append(
                    Finding(
                        checker=self.name,
                        code="EQUATION_NUMBER_GAP",
                        severity=Severity.WARNING,
                        message=f"公式编号不连续，缺少 {missing}。",
                        evidence={
                            "present": numbers,
                            "missing": missing,
                            "range": [min(numbers), max(numbers)],
                        },
                        hint="确认是否有公式被删除但编号未重排。",
                    )
                )
        return findings


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


__all__ = ["FormulaChecker"]
