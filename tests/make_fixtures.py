"""Build deliberately broken .docx fixtures, so the checkers can be proven to
fire — not just proven to stay quiet.

A checker that never reports anything passes every "no false positives" test and
is still useless. These fixtures each plant exactly one defect, which is what
lets a test assert "the right code fired, and only that code".
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import docx
from docx.shared import Pt

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _base(path: Path) -> docx.Document:
    """A clean one-paragraph document to mutate."""
    document = docx.Document()
    document.add_paragraph("这是一个正常段落。")
    return document


def healthy(path: Path) -> Path:
    """A document with nothing wrong with it."""
    document = _base(path)
    document.add_heading("一级标题", level=1)
    document.add_paragraph("正文内容。")
    document.add_heading("二级标题", level=2)
    document.add_paragraph("更多正文。")
    document.save(path)
    return path


def raw_latex(path: Path) -> Path:
    """A paragraph containing unrendered LaTeX."""
    document = _base(path)
    document.add_paragraph(r"求解得到 \frac{\partial u}{\partial t} = \nabla^2 u")
    document.save(path)
    return path


def unbalanced_dollar(path: Path) -> Path:
    """A paragraph with an odd number of ``$`` delimiters."""
    document = _base(path)
    document.add_paragraph("能量为 $E = mc^2 这个公式没有闭合")
    document.save(path)
    return path


def duplicate_equation_number(path: Path) -> Path:
    """Two equations sharing one number."""
    document = _base(path)
    document.add_paragraph("第一式 (1)")
    document.add_paragraph("第二式 (1)")
    document.save(path)
    return path


def equation_gap(path: Path) -> Path:
    """Equation numbers that skip a value."""
    document = _base(path)
    document.add_paragraph("第一式 (1)")
    document.add_paragraph("第二式 (3)")
    document.save(path)
    return path


def heading_without_outline(path: Path) -> Path:
    """A "heading" that is only bold text — no style, no outline level.

    The fake heading is placed *after* the title-block window (the first three
    paragraphs are exempt as cover matter), so it is reported.

    Built at the XML level because ``add_heading`` would add the very style the
    fixture is meant to lack.
    """
    document = _base(path)
    document.add_paragraph("正文第一段，用来把假标题推出标题块豁免范围。")
    document.add_paragraph("正文第二段。")
    para = document.add_paragraph()
    run = para.add_run("看起来像标题的一行")
    run.bold = True
    run.font.size = Pt(16)
    document.add_paragraph("假标题之后的正文。")
    document.save(path)
    return path


def no_headings(path: Path) -> Path:
    """A document with no headings at all."""
    document = _base(path)
    document.add_paragraph("只有正文，没有任何标题。")
    document.save(path)
    return path


def corrupt_package(path: Path) -> Path:
    """A file that claims to be .docx but is not a zip."""
    path.write_bytes(b"this is not a zip file, it is plain text")
    return path


def missing_styles_part(path: Path) -> Path:
    """A valid package with ``word/styles.xml`` removed."""
    good = FIXTURE_DIR / "_tmp_good.docx"
    healthy(good)
    with zipfile.ZipFile(good) as source:
        items = [(i, source.read(i.filename)) for i in source.infolist()]
    with zipfile.ZipFile(path, "w") as target:
        for info, data in items:
            if info.filename == "word/styles.xml":
                continue
            target.writestr(info, data)
    good.unlink(missing_ok=True)
    return path


BUILDERS = {
    "healthy": healthy,
    "raw_latex": raw_latex,
    "unbalanced_dollar": unbalanced_dollar,
    "duplicate_equation_number": duplicate_equation_number,
    "equation_gap": equation_gap,
    "heading_without_outline": heading_without_outline,
    "no_headings": no_headings,
    "corrupt_package": corrupt_package,
    "missing_styles_part": missing_styles_part,
}


def build_all(target: Path | None = None) -> dict[str, Path]:
    """Write every fixture and return name -> path."""
    target = target or FIXTURE_DIR
    target.mkdir(parents=True, exist_ok=True)
    return {name: builder(target / f"{name}.docx") for name, builder in BUILDERS.items()}


if __name__ == "__main__":
    for name, path in build_all().items():
        print(f"{name:32} {path.stat().st_size:>8,} B  {path}")
