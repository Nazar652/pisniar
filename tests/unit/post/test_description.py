"""The description: normalizing, and the one parser that gives the caption its segments and the save its problems."""

from __future__ import annotations

import pytest

from pisniar.post.description import (
    LABEL_OTHER_SITE_WARNING,
    MAX_DESCRIPTION_LINKS,
    NON_LATIN_HOST_WARNING,
    ParsedDescription,
    link_destinations,
    normalize_description,
    parse_description,
)
from pisniar.post.render import LineBreakSegment, LinkSegment, TextSegment


def shown(parsed: ParsedDescription) -> list[tuple[str, str]]:
    """a flat view of the segments: kind and what it draws."""
    view: list[tuple[str, str]] = []
    for segment in parsed.segments:
        if isinstance(segment, TextSegment):
            view.append(("text", segment.value))
        elif isinstance(segment, LinkSegment):
            view.append(("link", f"{segment.text} -> {segment.href}"))
        elif isinstance(segment, LineBreakSegment):
            view.append(("break", ""))
    return view


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("   \n \n", None),
        ("рядок", "рядок"),
        ("a\r\nb\rc", "a\nb\nc"),
        ("a   \nb\t", "a\nb"),
        ("\n\n a\n\n\n\nb\n\n", " a\n\nb"),
        ("a‮b﻿c⁦", "abc"),
        ("a b", "a b"),
    ],
)
def test_normalizing_cleans_the_edges_and_the_invisible_characters(raw: str | None, expected: str | None) -> None:
    assert normalize_description(raw) == expected


def test_normalizing_keeps_the_zero_width_joiner_of_a_family_emoji() -> None:
    family = "\U0001f468‍\U0001f469‍\U0001f467"

    assert normalize_description(f"сім'я {family}") == f"сім'я {family}"


def test_normalizing_is_idempotent() -> None:
    once = normalize_description("\r\n\r\nперший  \r\n\r\n\r\n[Слухати](https://example.com/a)\r\n")

    assert normalize_description(once) == once


def test_plain_text_and_paragraphs_become_text_and_line_breaks() -> None:
    parsed = parse_description("Перший рядок\nдругий\n\nНовий абзац")

    assert parsed.problems == []
    assert shown(parsed) == [
        ("text", "Перший рядок"),
        ("break", ""),
        ("text", "другий"),
        ("break", ""),
        ("break", ""),
        ("text", "Новий абзац"),
    ]


def test_a_link_is_a_segment_between_its_neighbours() -> None:
    parsed = parse_description("Дивіться [кліп](https://youtu.be/abc) і слухайте")

    assert parsed.problems == []
    assert shown(parsed) == [
        ("text", "Дивіться "),
        ("link", "кліп -> https://youtu.be/abc"),
        ("text", " і слухайте"),
    ]


def test_parentheses_inside_the_address_are_balanced() -> None:
    parsed = parse_description("[Вікі](https://uk.wikipedia.org/wiki/Гурт_(музика))!")

    assert parsed.problems == []
    assert shown(parsed)[0] == ("link", "Вікі -> https://uk.wikipedia.org/wiki/Гурт_(музика)")
    assert shown(parsed)[1] == ("text", "!")


@pytest.mark.parametrize(
    "text",
    [
        "https://example.com/bare",
        "<b>жирний</b> & <a href='https://x.com'>x</a>",
        "#тег @user",
        "[Інтро] і (дужки) та ]зворотні[",
        "[без адреси]",
        "**зірочки** __підкреслення__",
    ],
)
def test_everything_but_a_link_is_literal_text(text: str) -> None:
    parsed = parse_description(text)

    assert parsed.problems == []
    assert shown(parsed) == [("text", text)]


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("[Клік](javascript:alert(1))", "http://"),
        ("[Пошта](mailto:a@b.com)", "http://"),
        ("[Телеграм](tg://resolve?domain=x)", "http://"),
        ("[Файл](ftp://example.com/a)", "http://"),
        ("[Без схеми](example.com/a)", "http://"),
        ("[Порожньо]()", "немає адреси"),
        ("[](https://example.com)", "без тексту"),
        ("[ ​ ](https://example.com)", "без тексту"),
        ("[Хост](https:///path)", "немає адреси сайту"),
        ("[Хост](https://)", "немає адреси сайту"),
        ("[Логін](https://user:pass@example.com/)", "логін чи пароль"),
        ("[Логін](https://user@example.com/)", "логін чи пароль"),
        ("[Порт](https://example.com:99999/)", "некоректна"),
        ("[Пробіл](https://example.com/a b)", "пробіл"),
        ("[Рядок](https://example.com/a\nb)", "пробіл"),
        ("[Відкрите](https://example.com/a", "не закрито"),
        ("[Службові](https://exam‍ple.com/)", "службові символи"),
        ("[Довге](https://example.com/" + "a" * 2000 + ")", "задовга"),
        ("[" + "я" * 201 + "](https://example.com)", "задовгий"),
    ],
)
def test_a_broken_link_is_a_sentence_naming_it_and_stays_literal(text: str, fragment: str) -> None:
    parsed = parse_description(text)

    assert len(parsed.problems) == 1
    assert fragment in parsed.problems[0]
    assert not any(isinstance(segment, LinkSegment) for segment in parsed.segments)
    literal = "".join(segment.value for segment in parsed.segments if isinstance(segment, TextSegment))
    assert literal == (normalize_description(text) or "").replace("\n", "")


def test_a_problem_names_the_link_it_is_about() -> None:
    parsed = parse_description("[Купити квиток](javascript:x)")

    assert parsed.problems == ["Посилання «Купити квиток» має починатися з http:// або https://."]


def test_a_long_label_is_shortened_in_the_sentence() -> None:
    parsed = parse_description("[" + "ю" * 100 + "](ftp://a.com)")

    assert "ю" * 30 + "…" in parsed.problems[0]
    assert "ю" * 31 not in parsed.problems[0]


def test_each_broken_link_is_reported_and_a_good_one_still_works() -> None:
    parsed = parse_description("[Один](ftp://a.com) [Добре](https://b.com) [Три](mailto:c@d.com)")

    assert len(parsed.problems) == 2
    assert [kind for kind, _ in shown(parsed)] == ["text", "link", "text"]


def test_http_and_mixed_case_schemes_are_accepted() -> None:
    parsed = parse_description("[Один](http://a.com/x) [Два](HTTPS://b.com/y?q=1&r=2#z)")

    assert parsed.problems == []
    assert [segment.href for segment in parsed.segments if isinstance(segment, LinkSegment)] == [
        "http://a.com/x",
        "HTTPS://b.com/y?q=1&r=2#z",
    ]


def test_ten_links_are_allowed_and_the_eleventh_is_a_problem_shown_as_text() -> None:
    ten = " ".join(f"[{index}](https://example.com/{index})" for index in range(MAX_DESCRIPTION_LINKS))
    eleven = ten + " [last](https://example.com/last)"

    assert parse_description(ten).problems == []

    parsed = parse_description(eleven)
    assert parsed.problems == ["Забагато посилань: 11, дозволено не більше 10."]
    assert sum(isinstance(segment, LinkSegment) for segment in parsed.segments) == MAX_DESCRIPTION_LINKS
    assert "[last](https://example.com/last)" in shown(parsed)[-1][1]


def test_the_label_keeps_a_joiner_and_loses_bidi_controls() -> None:
    parsed = parse_description("[‮\U0001f468‍\U0001f469 дім](https://example.com)")

    link = parsed.segments[0]
    assert isinstance(link, LinkSegment)
    assert link.text == "\U0001f468‍\U0001f469 дім"


def test_the_destinations_name_the_real_host_of_each_valid_link_in_order() -> None:
    found = link_destinations("[Квиток](https://Tickets.Example.org/a?b=1) [Зламане](ftp://x.com) [Два](http://a.com)")

    assert [(link.label, link.host, link.warnings) for link in found] == [
        ("Квиток", "tickets.example.org", []),
        ("Два", "a.com", []),
    ]
    assert link_destinations(None) == []


def test_a_non_latin_host_is_shown_as_punycode_with_a_warning() -> None:
    [link] = link_destinations("[Банк](https://банк.укр/вхід)")

    assert link.host == "xn--80ab2al.xn--j1amh"
    assert link.warnings == [NON_LATIN_HOST_WARNING]


@pytest.mark.parametrize(
    "target",
    [
        "https://банк..укр/x",
        "https://" + "я" * 64 + ".укр/x",
        "https://example..com/x",
    ],
)
def test_a_host_that_does_not_encode_is_refused_and_stays_literal(target: str) -> None:
    text = f"[Банк]({target})"

    parsed = parse_description(text)

    assert parsed.problems == ["Адреса посилання «Банк» некоректна."]
    assert not any(isinstance(segment, LinkSegment) for segment in parsed.segments)
    assert "".join(s.value for s in parsed.segments if isinstance(s, TextSegment)) == text
    assert link_destinations(text) == []


@pytest.mark.parametrize(
    ("label", "target", "warned"),
    [
        ("spotify.com", "https://evil.example/x", True),
        ("https://www.spotify.com/track/1", "https://open.spotify.com/track/1", False),
        ("www.example.org", "https://example.org/a", False),
        ("example.org", "https://evil.example.org.attacker.net/a", True),
        ("Слухати на Spotify", "https://evil.example/x", False),
        ("версія 2.0", "https://example.org", False),
        ("spotify.com.", "https://evil.example/x", True),
        ("відкрити spotify.com", "https://evil.example/x", True),
        ("spotify.com тут", "https://evil.example/x", True),
        ("«spotify.com», тут", "https://evil.example/x", True),
        ("відкрити spotify.com", "https://open.spotify.com/x", False),
        ("spotify.com.", "https://www.spotify.com/x", False),
        ("просто слова, без адреси.", "https://evil.example/x", False),
    ],
)
def test_a_label_that_reads_as_another_site_is_flagged(label: str, target: str, warned: bool) -> None:
    [link] = link_destinations(f"[{label}]({target})")

    assert (LABEL_OTHER_SITE_WARNING in link.warnings) is warned


def test_an_empty_description_has_no_segments() -> None:
    assert parse_description(None) == ParsedDescription([], [])
    assert parse_description(" \n ") == ParsedDescription([], [])
