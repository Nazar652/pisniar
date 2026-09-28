"""The caption document model and its serializers. A structured document, not a
string: Telegram counts the limit on *visible* text (UTF-16 code units), while
URLs live in HTML entities and cost only their link text.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pisniar.post.sanitize import (
    escape_attribute,
    escape_text,
    is_safe_href,
    sanitize_metadata_text,
)


@dataclass(frozen=True)
class TextSegment:
    value: str


@dataclass(frozen=True)
class LinkSegment:
    text: str
    href: str


@dataclass(frozen=True)
class LineBreakSegment:
    pass


Segment = TextSegment | LinkSegment | LineBreakSegment


@dataclass
class CaptionDocument:
    segments: list[Segment] = field(default_factory=list)

    def append_text(self, value: str) -> None:
        """Sanitizes on the way in, not at render time, so `visible_length()`
        counts what Telegram will actually show."""
        sanitized = sanitize_metadata_text(value)
        if sanitized:
            self.segments.append(TextSegment(sanitized))

    def append_link(self, text: str, href: str) -> None:
        sanitized = sanitize_metadata_text(text)
        if sanitized:
            self.segments.append(LinkSegment(text=sanitized, href=href))

    def append_line_break(self) -> None:
        self.segments.append(LineBreakSegment())


class UnsafeHrefError(ValueError):
    """Unreachable given `urls.classify_url()`'s own check; the last line of
    defense at render time."""


def utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def visible_length(document: CaptionDocument) -> int:
    total = 0
    for segment in document.segments:
        if isinstance(segment, TextSegment):
            total += utf16_length(segment.value)
        elif isinstance(segment, LinkSegment):
            total += utf16_length(segment.text)
        elif isinstance(segment, LineBreakSegment):
            total += 1
    return total


def plain_text(document: CaptionDocument) -> str:
    parts: list[str] = []
    for segment in document.segments:
        if isinstance(segment, TextSegment):
            parts.append(segment.value)
        elif isinstance(segment, LinkSegment):
            parts.append(segment.text)
        elif isinstance(segment, LineBreakSegment):
            parts.append("\n")
    return "".join(parts)


def render_html(document: CaptionDocument) -> str:
    parts: list[str] = []
    for segment in document.segments:
        if isinstance(segment, TextSegment):
            parts.append(escape_text(segment.value))
        elif isinstance(segment, LinkSegment):
            if not is_safe_href(segment.href):
                raise UnsafeHrefError(f"unsafe href scheme: {segment.href!r}")
            href = escape_attribute(segment.href)
            parts.append(f'<a href="{href}">{escape_text(segment.text)}</a>')
        elif isinstance(segment, LineBreakSegment):
            parts.append("\n")
    return "".join(parts)
