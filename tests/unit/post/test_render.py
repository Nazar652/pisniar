"""`render`: UTF-16 visible length (never `len()` on the HTML string), the `🇺🇦` = 4 units check that appears in
every post, and `href` scheme enforcement at the last line of defense."""

from __future__ import annotations

import pytest

from pisniar.post.render import (
    CaptionDocument,
    UnsafeHrefError,
    plain_text,
    render_html,
    utf16_length,
    visible_length,
)


def test_utf16_length_of_flag_emoji_is_four() -> None:
    # U+1F1FA U+1F1E6, each above the BMP -> a surrogate pair (2 units) each.
    assert utf16_length("🇺🇦") == 4


def test_utf16_length_of_ascii_matches_python_len() -> None:
    assert utf16_length("hello") == len("hello")


def test_visible_length_counts_link_text_not_url() -> None:
    document = CaptionDocument()
    document.append_link("Spotify", "https://open.spotify.com/track/aVeryLongIdThatWouldBlowThePlainUrlLength")

    # "Spotify" is 7 UTF-16 units; the ~60-char URL must not be counted.
    assert visible_length(document) == 7


def test_visible_length_counts_line_break_as_one_unit() -> None:
    document = CaptionDocument()
    document.append_text("a")
    document.append_line_break()
    document.append_text("b")

    assert visible_length(document) == 3


def test_visible_length_does_not_count_html_escape_sequences() -> None:
    document = CaptionDocument()
    document.append_text("<Tom & Jerry>")

    # visible text is 13 chars even though the HTML-escaped form is longer.
    assert visible_length(document) == 13
    assert len(render_html(document)) > 13


def test_plain_text_joins_segments_with_newlines_for_line_breaks() -> None:
    document = CaptionDocument()
    document.append_text("Artist — Title")
    document.append_line_break()
    document.append_link("Spotify", "https://open.spotify.com/track/x")

    assert plain_text(document) == "Artist — Title\nSpotify"


def test_render_html_escapes_text_content() -> None:
    document = CaptionDocument()
    document.append_text('<script>alert("x")</script>')

    html = render_html(document)

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_render_html_produces_a_named_link() -> None:
    document = CaptionDocument()
    document.append_link("Spotify", "https://open.spotify.com/track/x")

    html = render_html(document)

    assert html == '<a href="https://open.spotify.com/track/x">Spotify</a>'


def test_render_html_escapes_ampersand_in_href() -> None:
    document = CaptionDocument()
    document.append_link("Link", "https://example.com/?a=1&b=2")

    html = render_html(document)

    assert 'href="https://example.com/?a=1&amp;b=2"' in html


def test_render_html_rejects_unsafe_href_scheme() -> None:
    document = CaptionDocument()
    document.append_link("Click me", "javascript:alert(1)")

    with pytest.raises(UnsafeHrefError):
        render_html(document)


def test_rendered_html_never_contains_a_bidi_override_from_metadata() -> None:
    """end to end: a title carrying `U+202E` must not reach Telegram."""
    document = CaptionDocument()
    document.append_text("Artist \u202e— Title")

    rendered = render_html(document)

    assert "\u202e" not in rendered
    assert rendered == "Artist — Title"


def test_rendered_html_never_contains_a_raw_newline_from_metadata() -> None:
    document = CaptionDocument()
    document.append_text("Title\n#FakeHashtag")

    rendered = render_html(document)

    assert rendered == "Title #FakeHashtag"


def test_line_break_segments_still_produce_real_newlines() -> None:
    """only metadata is folded — the document's own structure is not."""
    document = CaptionDocument()
    document.append_text("first")
    document.append_line_break()
    document.append_text("second")

    assert render_html(document) == "first\nsecond"


def test_link_text_is_sanitized_too() -> None:
    document = CaptionDocument()
    document.append_link("Spo\u200btify", "https://open.spotify.com/track/x")

    assert "\u200b" not in render_html(document)
