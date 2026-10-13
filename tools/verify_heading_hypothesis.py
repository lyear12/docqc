"""Verify whether HEADING_WITHOUT_OUTLINE_LEVEL is a false positive.

Hypothesis: Word heading styles define `w:outlineLvl` inside the *style*
definition in styles.xml, and the paragraph inherits it. If so, checking only
the paragraph's `w:pPr` produces 40 false warnings on a perfectly normal
document.
"""

import zipfile
import re
from pathlib import Path

DOC = Path(r"C:\Users\31887\dsh\问题四完整解答_修改意见.docx")
NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

with zipfile.ZipFile(DOC) as z:
    styles = z.read("word/styles.xml").decode("utf-8")
    document = z.read("word/document.xml").decode("utf-8")

print("=== styles.xml 里 Heading 样式的 outlineLvl 定义 ===")
# 找出每个 w:style，看它的 styleId 和是否含 outlineLvl
for match in re.finditer(r"<w:style\b[^>]*>.*?</w:style>", styles, re.S):
    block = match.group(0)
    sid = re.search(r'w:styleId="([^"]+)"', block)
    if not sid:
        continue
    name = sid.group(1)
    if "eading" not in name and "标题" not in name:
        continue
    outline = re.search(r'<w:outlineLvl\s+w:val="(\d+)"', block)
    based = re.search(r'<w:basedOn\s+w:val="([^"]+)"', block)
    print(
        f"  styleId={name:24} outlineLvl={outline.group(1) if outline else '(未定义)':>10}"
        f"  basedOn={based.group(1) if based else '-'}"
    )

print()
print("=== 段落层：第一个 Heading 段落的 pPr 内容 ===")
m = re.search(r'<w:p\b[^>]*>(?:(?!</w:p>).)*?<w:pStyle\s+w:val="Heading1".*?</w:p>', document, re.S)
if m:
    para = m.group(0)
    ppr = re.search(r"<w:pPr>.*?</w:pPr>", para, re.S)
    print("  " + (ppr.group(0)[:500] if ppr else "(无 pPr)"))
else:
    print("  (未找到 Heading1 段落)")

print()
print("=== 结论 ===")
has_style_outline = bool(re.search(r'w:styleId="Heading1".*?w:outlineLvl', styles, re.S))
print(f"  Heading1 在 styles.xml 中定义了大纲级别？ {has_style_outline}")
print(f"  → 若为 True，则段落层没有 outlineLvl 属正常继承，那 40 条警告是【误报】")
