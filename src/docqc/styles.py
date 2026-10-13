"""Resolve effective paragraph formatting through Word's style hierarchy.

Word cascades formatting: a paragraph's look is the result of

    document defaults -> style chain (basedOn -> basedOn -> ...) -> direct formatting

A checker that reads only the paragraph's own XML therefore misses most of what
the reader sees. Concretely: ``<w:pStyle w:val="Heading1"/>`` carries no
``w:outlineLvl``, yet the paragraph *is* at outline level 0 because
``Heading1`` defines it in ``styles.xml``. Reading just the paragraph yields 40
false "heading without outline level" warnings on a perfectly healthy thesis —
which is how this module came to exist.

The resolution order implemented here matches what Word does:

1. Direct formatting on the paragraph (``w:pPr/w:outlineLvl``) wins.
2. Otherwise follow ``w:pStyle`` into ``styles.xml``.
3. If that style has no value, follow its ``w:basedOn`` chain, nearest ancestor
   first, and take the first value found.

``w:outlineLvl`` is the only property resolved today, because it is the only one
a checker needs. The machinery is generic: adding a property means adding a
field to :class:`StyleInfo` and one entry to :data:`_INHERITED_PROPS`.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

#: ``w:outlineLvl`` on a style definition, with its parent style id.
_STYLE_BLOCK = re.compile(r"<w:style\b([^>]*)>(.*?)</w:style>", re.S)
_STYLE_ID = re.compile(r'w:styleId="([^"]+)"')
_STYLE_TYPE = re.compile(r'w:type="([^"]+)"')
_BASED_ON = re.compile(r'<w:basedOn\s+w:val="([^"]+)"')
_OUTLINE_IN_BLOCK = re.compile(r'<w:outlineLvl\s+w:val="(\d+)"')
_STYLE_NAME = re.compile(r'<w:name\s+w:val="([^"]+)"')

#: Properties resolved through the style chain. Extend here to inherit more.
_INHERITED_PROPS = ("outline_level",)


@dataclass(slots=True)
class StyleInfo:
    """One style definition, as far as the checkers care.

    Attributes:
        style_id: The ``w:styleId`` used by ``w:pStyle`` references.
        name: The human-facing ``w:name`` (localized, e.g. ``标题 1``).
        based_on: Parent style id, or ``None`` at the root of the chain.
        style_type: ``paragraph``, ``character``, ``table``, or ``numbering``.
        outline_level: ``w:outlineLvl`` declared on this style, if any.
    """

    style_id: str
    name: str | None = None
    based_on: str | None = None
    style_type: str | None = None
    outline_level: int | None = None


class StyleTable:
    """The document's style definitions, with inheritance resolution.

    Built once per document from ``word/styles.xml`` and then queried per
    paragraph. Cycles in ``basedOn`` are tolerated (broken documents exist) by
    capping the walk depth.

    Args:
        styles: Style definitions keyed by ``w:styleId``.
    """

    #: Guards against a ``basedOn`` cycle rather than trusting the document.
    _MAX_CHAIN = 32

    def __init__(self, styles: dict[str, StyleInfo]) -> None:
        self._styles = styles

    @classmethod
    def from_docx(cls, path: Path) -> StyleTable:
        """Parse ``word/styles.xml`` out of a ``.docx``.

        A missing or unreadable styles part yields an empty table rather than an
        error: a document without styles still needs to be checkable, and the
        structure checker reports the missing part separately.

        Args:
            path: The ``.docx`` file.

        Returns:
            The parsed table; empty when styles are absent.
        """
        try:
            with zipfile.ZipFile(path) as archive:
                raw = archive.read("word/styles.xml")
        except (zipfile.BadZipFile, KeyError, OSError):
            return cls({})

        text = raw.decode("utf-8", "replace")
        styles: dict[str, StyleInfo] = {}
        for attributes, body in _STYLE_BLOCK.findall(text):
            id_match = _STYLE_ID.search(attributes)
            if id_match is None:
                continue
            style_id = id_match.group(1)
            type_match = _STYLE_TYPE.search(attributes)
            name_match = _STYLE_NAME.search(body)
            based_match = _BASED_ON.search(body)
            outline_match = _OUTLINE_IN_BLOCK.search(body)
            styles[style_id] = StyleInfo(
                style_id=style_id,
                name=name_match.group(1) if name_match else None,
                based_on=based_match.group(1) if based_match else None,
                style_type=type_match.group(1) if type_match else None,
                outline_level=int(outline_match.group(1)) if outline_match else None,
            )
        return cls(styles)

    def get(self, style_id: str | None) -> StyleInfo | None:
        """Look up one style.

        Args:
            style_id: The ``w:styleId`` to find.

        Returns:
            The style, or ``None`` when unset or unknown.
        """
        if not style_id:
            return None
        return self._styles.get(style_id)

    def resolve(self, style_id: str | None, prop: str) -> int | None:
        """Resolve an inherited numeric property along the ``basedOn`` chain.

        Walks from the named style towards the root and returns the first
        declared value, which is what Word renders.

        Args:
            style_id: Starting style id.
            prop: Attribute name on :class:`StyleInfo`; must be in
                :data:`_INHERITED_PROPS`.

        Returns:
            The inherited value, or ``None`` when no style in the chain sets it.

        Raises:
            ValueError: If ``prop`` is not an inheritable property. Failing
                loudly beats silently returning ``None`` for a typo.
        """
        if prop not in _INHERITED_PROPS:
            raise ValueError(
                f"{prop!r} is not inheritable; known: {', '.join(_INHERITED_PROPS)}"
            )
        seen: set[str] = set()
        current = style_id
        for _ in range(self._MAX_CHAIN):
            if not current or current in seen:
                break
            seen.add(current)
            style = self._styles.get(current)
            if style is None:
                break
            value = getattr(style, prop, None)
            if value is not None:
                return int(value)
            current = style.based_on
        return None

    def __len__(self) -> int:
        """Number of style definitions."""
        return len(self._styles)

    def __contains__(self, style_id: object) -> bool:
        """Whether a style id is defined."""
        return style_id in self._styles


@dataclass(slots=True)
class ResolvedParagraphStyle:
    """What a paragraph's formatting actually resolves to.

    Attributes:
        style_id: Direct ``w:pStyle`` value, if any.
        style_name: The style's ``w:name``, if the style is known.
        outline_level: Effective outline level after inheritance.
        source: Where the value came from — ``"direct"``, ``"style:Heading1"``,
            or ``None`` when nothing declares it.
    """

    style_id: str | None = None
    style_name: str | None = None
    outline_level: int | None = None
    source: str | None = None
    chain: list[str] = field(default_factory=list)


__all__ = ["ResolvedParagraphStyle", "StyleInfo", "StyleTable"]
