# docqc

**文档质检工具** —— 在交付前自动检查 Word 文档的排版、公式和结构问题。

[![CI](https://github.com/lyear12/docqc/actions/workflows/ci.yml/badge.svg)](https://github.com/lyear12/docqc/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

---

## 为什么做这个

学术论文交付前，作者通常要反复做同一件事：

```
导出 PDF → 逐页转成 PNG → 一页页看 → 发现问题 → 改 → 全部重来
```

一篇 50 页的论文，这个过程要走几十遍。更麻烦的是，**有些问题肉眼看不出**：

| 问题 | 为什么肉眼难发现 |
|---|---|
| 裸 LaTeX 排进正文 | `\frac{a}{b}` 在 Word 里显示为字面文本，扫读时容易漏过 |
| 标题没有大纲级别 | 看起来是标题，但目录里不出现 |
| `w:pPr` 子元素顺序错误 | Python 能解析，**Word 直接拒绝打开** |
| 公式编号跳号 | 编号散落在几十页里，不逐个核对发现不了 |

`docqc` 把这件事变成一条命令。

## 快速开始

```bash
# 安装
pip install -e ".[all]"

# 检查一个文档
docqc check 论文.docx

# 只看结构问题
docqc check 论文.docx --only structure

# 输出 JSON（供下游程序消费）
docqc check 论文.docx --json > report.json

# 看看本机有哪些渲染后端
docqc backends
```

## 检查器

| 名称 | 检查内容 | 需要渲染 |
|---|---|---|
| `layout` | 空白页、内容溢出页边、页面近空 | ✅ 是 |
| `formula` | 裸 LaTeX、`$` 不配对、公式编号跳号/重复 | ❌ 否 |
| `structure` | 标题样式、大纲级别、层级跳级、`w:pPr` 顺序、包完整性 | ❌ 否 |

三个检查器相互独立。`layout` 需要 Word 或 LibreOffice；另两个只读 XML，**任何机器都能跑**。

## 输出示例

```
╭─ 文档 ─────────────────────────────────────────────╮
│ 问题四完整解答.docx                                  │
│ C:\Users\...\问题四完整解答.docx                     │
│ 68,302 B · sha256 a3f2b1c9d8e7… · 208 段 · 4 表     │
╰────────────────────────────────────────────────────╯

 检查器       状态      错误   警告   耗时
 structure   有警告        ·      3    12 ms
 formula     不通过        2      ·     4 ms
 layout      通过          ·      ·   8420 ms

  错误  ¶42  段落含未渲染的 LaTeX 源码…
        formula · RAW_LATEX_IN_TEXT
        → 用 Word 公式编辑器重排

╭────────────────────────────────────────────────────╮
│ 不通过 · 2 错误 · 3 警告                            │
╰────────────────────────────────────────────────────╯
```

## 架构

```
src/docqc/
├── models.py      词汇表：Finding / CheckResult / Report（无依赖）
├── ooxml.py       原生 WordprocessingML 助手（无依赖）
├── document.py    格式无关的文档视图
├── render.py      docx → PDF → PNG
├── registry.py    检查器插件协议
├── checkers/      每个检查器一个模块
├── runner.py      运行检查器、组装报告
└── cli.py         命令行入口
```

**分层原则**：每一层都能独立理解和测试。`models` 和 `ooxml` 没有任何内部依赖；
`checkers` 只依赖 `document` 和 `models`，不碰渲染。

### 加一个检查器

只改一个文件：

```python
# src/docqc/checkers/my_check.py
from ..registry import register
from ..models import CheckResult, Finding, Severity, Status

@register("my_check", "我的检查", order=40)
class MyChecker:
    name = "my_check"
    description = "我的检查"

    def run(self, document):
        findings = []
        if something_wrong:
            findings.append(Finding(
                checker=self.name,
                code="SOME_CODE",
                severity=Severity.WARNING,
                message="说清哪里不对",
                evidence={"raw": "支撑这个判断的原始数据"},
            ))
        return CheckResult(
            checker=self.name,
            status=Status.WARN if findings else Status.PASS,
            findings=findings,
        )
```

然后在 `checkers/__init__.py` 里 import 一次即可。

## 环境要求

- **Python 3.11+**（已在 3.14 上验证）
- **渲染（可选）**：Word（COM，Windows）或 LibreOffice；PyMuPDF 或 poppler 的 `pdftoppm`
- 无渲染后端时 `layout` 会跳过自己并说明原因，**其余检查器照常工作**

## 设计原则

1. **发现必须可操作** —— 每条问题都有位置、证据和修复建议
2. **失败是一等结果** —— 检查器崩溃不影响其他检查器，报告照常产出
3. **宁可漏报不可误报** —— 一条假警报比漏掉一个边界情况更伤信任
4. **不编造严重级别** —— 三级含义固定：`ERROR` 必须修、`WARNING` 该修、`INFO` 只是告知

## 开发

```bash
pip install -e ".[all]"
pytest                    # 跑测试
ruff check .              # 格式检查
mypy src                  # 类型检查
```

## 许可证

[MIT](LICENSE)
