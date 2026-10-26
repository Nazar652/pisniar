"""The post description: plain text with `[text](https://…)` links and nothing else. One parser serves the save, which
refuses `problems`, and the caption, which shows a failed link as the literal text it was typed as.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlsplit

from pisniar.post.render import LineBreakSegment, LinkSegment, Segment, TextSegment
from pisniar.post.sanitize import CONTROL_CHARACTER_CATEGORIES, sanitize_description_text
from pisniar.resolve.urls import MAX_NORMALIZED_URL_BYTES, is_safe_url, is_within_length_limit

MAX_DESCRIPTION_LINKS = 10
MAX_LINK_LABEL_LENGTH = 200
LABEL_PREVIEW_LENGTH = 30

LABEL_HOST_PATTERN = re.compile(r"^(?:https?://)?((?:[^\s/:?#.]+\.)+[^\s/:?#.]{2,})(?:[/:?#].*)?$")
LABEL_TOKEN_PUNCTUATION = ".,;:!?\"'«»“”„‘’()[]{}<>"
NON_LATIN_HOST_WARNING = "адреса містить нелатинські літери"
LABEL_OTHER_SITE_WARNING = "текст посилання показує інший сайт"
NO_LABEL_PROBLEM = "Посилання без тексту: напишіть текст у квадратних дужках."
TOO_MANY_LINKS_PROBLEM = "Забагато посилань: {count}, дозволено не більше {limit}."


@dataclass(frozen=True)
class ParsedDescription:
    """`segments` always draw something; every `problems` entry is a sentence for the admin."""

    segments: list[Segment]
    problems: list[str]


@dataclass(frozen=True)
class ScannedLink:
    label: str
    target: str | None
    end: int
    problem: str | None


def normalize_description(raw: str | None) -> str | None:
    """line breaks as LF, control and bidi characters gone, no trailing spaces, no blank lines at the edges, at most
    one blank line in a row; nothing left is `None`."""
    if raw is None:
        return None

    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    kept: list[str] = []
    for line in text.split("\n"):
        cleaned = sanitize_description_text(line).rstrip()
        if cleaned == "" and (not kept or kept[-1] == ""):
            continue
        kept.append(cleaned)
    while kept and kept[-1] == "":
        kept.pop()
    return "\n".join(kept) or None


def shown(label: str) -> str:
    cleaned = sanitize_description_text(label).strip()
    if len(cleaned) > LABEL_PREVIEW_LENGTH:
        return cleaned[:LABEL_PREVIEW_LENGTH] + "…"
    return cleaned


def target_problem(label: str, target: str) -> str | None:
    name = shown(label)
    if not target:
        return f"У посиланні «{name}» немає адреси."
    if any(unicodedata.category(character) in CONTROL_CHARACTER_CATEGORIES for character in target):
        return f"Адреса посилання «{name}» містить службові символи."
    if not is_within_length_limit(target):
        return f"Адреса посилання «{name}» задовга: понад {MAX_NORMALIZED_URL_BYTES} байтів."
    try:
        parts = urlsplit(target)
        hostname = parts.hostname
        _ = parts.port  # raises on a malformed port
    except ValueError:
        return f"Адреса посилання «{name}» некоректна."
    if not is_safe_url(target):
        return f"Посилання «{name}» має починатися з http:// або https://."
    if not hostname:
        return f"У посиланні «{name}» немає адреси сайту."
    if "@" in parts.netloc:
        return f"Посилання «{name}» не може містити логін чи пароль."
    try:
        hostname.encode("idna")
    except UnicodeError:
        return f"Адреса посилання «{name}» некоректна."
    return None


def label_problem(label: str) -> str | None:
    cleaned = sanitize_description_text(label).strip()
    if not cleaned:
        return NO_LABEL_PROBLEM
    if len(cleaned) > MAX_LINK_LABEL_LENGTH:
        return f"Текст посилання «{shown(cleaned)}» задовгий: {len(cleaned)} з {MAX_LINK_LABEL_LENGTH} символів."
    return None


def scan_link(text: str, start: int) -> ScannedLink | None:
    """reads `[label](target)` at `start`; `None` when it is not shaped like a link at all (`[Intro]` stays text)."""
    close = start + 1
    while close < len(text) and text[close] not in "[]\n":
        close += 1
    if close + 1 >= len(text) or text[close] != "]" or text[close + 1] != "(":
        return None

    label = text[start + 1 : close]
    name = shown(label) or "…"
    opening = close + 2
    depth = 1
    position = opening
    while position < len(text):
        character = text[position]
        if character.isspace():
            problem = f"Адреса посилання «{name}» містить пробіл або обрив рядка."
            return ScannedLink(label, None, position, problem)
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return ScannedLink(label, text[opening:position], position + 1, None)
        position += 1
    return ScannedLink(label, None, position, f"Посилання «{name}» не закрито дужкою «)».")


@dataclass(frozen=True)
class LinkDestination:
    """where a link really goes: the host in ASCII (punycode for a non-Latin one) and what looks off about it."""

    label: str
    host: str
    warnings: list[str]


def ascii_host(host: str) -> str:
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def bare_host(host: str) -> str:
    return host.removeprefix("www.")


def label_names_another_site(label: str, host: str) -> bool:
    """a word of the label reads as an address whose host is not the target's (or one of its parents)."""
    target = bare_host(host)
    for token in label.lower().split():
        match = LABEL_HOST_PATTERN.match(token.strip(LABEL_TOKEN_PUNCTUATION))
        if match is None:
            continue
        shown_host = bare_host(match.group(1))
        if target != shown_host and not target.endswith("." + shown_host):
            return True
    return False


def link_destinations(raw: str | None) -> list[LinkDestination]:
    """one entry per valid link of the description, in order; warnings only, never a refusal."""
    destinations: list[LinkDestination] = []
    for segment in parse_description(raw).segments:
        if not isinstance(segment, LinkSegment):
            continue
        host = (urlsplit(segment.href).hostname or "").lower()
        warnings: list[str] = []
        shown = ascii_host(host)
        if not host.isascii():
            warnings.append(NON_LATIN_HOST_WARNING)
        if label_names_another_site(segment.text, host):
            warnings.append(LABEL_OTHER_SITE_WARNING)
        destinations.append(LinkDestination(segment.text, shown, warnings))
    return destinations


def parse_description(raw: str | None) -> ParsedDescription:
    text = normalize_description(raw) or ""
    segments: list[Segment] = []
    problems: list[str] = []
    pending: list[str] = []
    link_count = 0

    def flush_text() -> None:
        value = sanitize_description_text("".join(pending))
        pending.clear()
        if value:
            segments.append(TextSegment(value))

    position = 0
    while position < len(text):
        character = text[position]
        if character == "\n":
            flush_text()
            segments.append(LineBreakSegment())
            position += 1
            continue

        scanned = scan_link(text, position) if character == "[" else None
        if scanned is None:
            pending.append(character)
            position += 1
            continue

        target = scanned.target
        problem = scanned.problem
        if problem is None and target is not None:
            problem = label_problem(scanned.label) or target_problem(scanned.label, target)
        if problem is not None or target is None:
            if problem is not None:
                problems.append(problem)
            pending.append(character)
            position += 1
            continue

        link_count += 1
        if link_count > MAX_DESCRIPTION_LINKS:
            pending.append(text[position : scanned.end])
        else:
            flush_text()
            segments.append(LinkSegment(text=sanitize_description_text(scanned.label).strip(), href=target))
        position = scanned.end

    flush_text()
    if link_count > MAX_DESCRIPTION_LINKS:
        problems.append(TOO_MANY_LINKS_PROBLEM.format(count=link_count, limit=MAX_DESCRIPTION_LINKS))
    return ParsedDescription(segments, problems)
