"""Inspect the acceptance findings well enough to judge them.

A finding is only useful if a human agrees it is real. This prints, for the
documents with the most warnings, the *actual paragraphs* behind the top codes
so they can be read in context.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

ACCEPT = Path(r"C:\Users\31887\docqc\.docqc\acceptance")

TARGETS = {
    "低空经济-数据采集报告": ["SUSPECTED_FAKE_HEADING", "HEADING_WITHOUT_OUTLINE_LEVEL"],
    "问题一二正文": ["UNBALANCED_MATH_DELIMITER", "HEADING_WITHOUT_OUTLINE_LEVEL"],
    "问题四完整解答": ["SUSPECTED_FAKE_HEADING"],
}

for label, codes in TARGETS.items():
    path = ACCEPT / f"{label}.json"
    if not path.is_file():
        print(f"missing {path.name}")
        continue
    report = json.loads(path.read_text(encoding="utf-8"))
    print("=" * 100)
    print(f"文档：{label}")
    print("=" * 100)

    grouped: dict[str, list[dict]] = defaultdict(list)
    for result in report["results"]:
        for finding in result["findings"]:
            if finding["code"] in codes:
                grouped[finding["code"]].append(finding)

    for code in codes:
        items = grouped.get(code, [])
        if not items:
            continue
        print(f"\n--- {code}（共 {len(items)} 条，展示前 8 条）")
        for finding in items[:8]:
            loc = finding["location"]
            where = " ".join(
                f"{k}={v}" for k, v in loc.items() if v is not None and k != "text"
            ) or "(document)"
            print(f"\n   位置: {where}")
            print(f"   消息: {finding['message']}")
            text = loc.get("text")
            if text:
                print(f"   原文: {text!r}")
            ev = finding.get("evidence", {})
            keep = {
                k: ev[k]
                for k in ("style", "style_id", "max_font_pt", "centred", "length", "chain")
                if k in ev
            }
            if keep:
                print(f"   证据: {keep}")
    print()
