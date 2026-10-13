"""One-off: tighten the ``list`` annotations in the structure checker.

mypy --strict requires a type argument; these three signatures take paragraphs,
so ``list[Paragraph]`` is both stricter and more readable.
"""

import pathlib

PATH = pathlib.Path(__file__).resolve().parents[1] / "src" / "docqc" / "checkers" / "structure.py"

REPLACEMENTS = [
    (
        "def _check_fake_headings(self, paragraphs: list) -> list[Finding]:",
        "def _check_fake_headings(self, paragraphs: list[Paragraph]) -> list[Finding]:",
    ),
    (
        "def _check_headings(self, document: Document, paragraphs: list) -> list[Finding]:",
        "def _check_headings(\n"
        "        self, document: Document, paragraphs: list[Paragraph]\n"
        "    ) -> list[Finding]:",
    ),
    (
        "def _check_property_order(self, paragraphs: list) -> list[Finding]:",
        "def _check_property_order(self, paragraphs: list[Paragraph]) -> list[Finding]:",
    ),
]

text = PATH.read_text(encoding="utf-8")
for old, new in REPLACEMENTS:
    if old not in text:
        raise SystemExit(f"pattern not found: {old[:60]}")
    text = text.replace(old, new)
PATH.write_text(text, encoding="utf-8")
print(f"patched {PATH.name}")
