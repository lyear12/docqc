"""Run docqc over a representative set of the user's real documents.

Purpose is acceptance, not a demo: the question is whether the findings are
*correct* on documents whose defects are already known. Anything reported here
must be traceable to something a human would agree is wrong.

Writes a JSON report per document plus a summary table, all under
``.docqc/acceptance/`` so the run is inspectable afterwards.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from docqc.runner import check

DOCS = [
    (
        "问题四完整解答",
        r"C:\Users\31887\Desktop\数学建模国赛\B题\新建文件夹\问题四完整解答.docx",
    ),
    (
        "问题一二正文",
        r"C:\Users\31887\Desktop\数学建模国赛\B题\问题一二正文资料\问题一二正文.docx",
    ),
    (
        "问题三改写版",
        r"C:\Users\31887\Desktop\数学建模国赛\最终优化\问题三改写版.docx",
    ),
    (
        "低空经济-七章报告",
        r"C:\Users\31887\dsh\低空经济\低空经济创新维度分析\output\七章报告.docx",
    ),
    (
        "低空经济-数据采集报告",
        r"C:\Users\31887\dsh\低空经济\创新维度\01_报告\创新维度_数据采集与处理报告.docx",
    ),
    (
        "低空经济-讲稿",
        r"C:\Users\31887\dsh\低空经济\创新维度\03_讲稿\创新维度_讲稿.docx",
    ),
]

OUT = Path(r"C:\Users\31887\docqc\.docqc\acceptance")
OUT.mkdir(parents=True, exist_ok=True)

# structure + formula only: both are render-free, so a full sweep stays fast.
# layout is exercised separately because it needs a Word COM round-trip per file.
NAMES = ["structure", "formula"]

rows = []
for label, raw_path in DOCS:
    path = Path(raw_path)
    if not path.is_file():
        print(f"SKIP  {label}: not found")
        continue

    started = time.perf_counter()
    try:
        report = check(path, names=NAMES)
    except Exception as exc:  # noqa: BLE001 - acceptance must not abort
        print(f"ERROR {label}: {type(exc).__name__}: {exc}")
        continue
    elapsed = time.perf_counter() - started

    payload = json.loads(report.model_dump_json())
    (OUT / f"{label}.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    doc = report.document
    rows.append(
        {
            "label": label,
            "paragraphs": doc.paragraph_count,
            "tables": doc.table_count,
            "status": report.summary.status.value,
            "errors": report.summary.errors,
            "warnings": report.summary.warnings,
            "seconds": round(elapsed, 2),
            "report": report,
        }
    )

print()
print("=" * 100)
print(
    f"{'文档':<24} {'段':>6} {'表':>5} {'状态':<8} {'错误':>5} {'警告':>5} {'秒':>6}"
)
print("-" * 100)
for row in rows:
    print(
        f"{row['label']:<24} {row['paragraphs'] or 0:>6} {row['tables'] or 0:>5} "
        f"{row['status']:<8} {row['errors']:>5} {row['warnings']:>5} {row['seconds']:>6}"
    )
print("=" * 100)

print("\n\n=== 各类发现汇总 ===")
from collections import Counter

counter: Counter[str] = Counter()
for row in rows:
    for finding in row["report"].findings():
        if finding.severity.value in {"error", "warning"}:
            counter[f"[{finding.severity.value}] {finding.checker} · {finding.code}"] += 1

for code, count in counter.most_common():
    print(f"  {count:>4}  {code}")

print(f"\nJSON 报告目录: {OUT}")
