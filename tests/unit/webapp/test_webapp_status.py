"""What the admin may do with a post in each state: the label, the verdict and the sentence.
The transition table is pinned too, because the verdict is read off it, never changed by it.
"""

from __future__ import annotations

import pytest

from pisniar.db.states import ALLOWED_TRANSITIONS, PostStatus
from pisniar.webapp import status
from pisniar.webapp.status import STATUS_LABELS, choosability_of, editability_of

EXPECTED_TRANSITIONS = {
    PostStatus.DRAFT: {PostStatus.RESOLVING, PostStatus.ARCHIVED},
    PostStatus.RESOLVING: {
        PostStatus.RESOLVED,
        PostStatus.FAILED,
        PostStatus.RESOLVING,
        PostStatus.ARCHIVED,
    },
    PostStatus.RESOLVED: {
        PostStatus.PUBLISHING,
        PostStatus.EDITING,
        PostStatus.DOWNLOADING,
        PostStatus.ARCHIVED,
    },
    PostStatus.DOWNLOADING: {
        PostStatus.DOWNLOADED,
        PostStatus.FAILED,
        PostStatus.DOWNLOADING,
        PostStatus.ARCHIVED,
    },
    PostStatus.DOWNLOADED: {PostStatus.EDITING, PostStatus.RENDERING, PostStatus.FAILED},
    PostStatus.EDITING: {PostStatus.RENDERING, PostStatus.RESOLVED, PostStatus.ARCHIVED},
    PostStatus.RENDERING: {
        PostStatus.RENDERED,
        PostStatus.FAILED,
        PostStatus.RENDERING,
        PostStatus.ARCHIVED,
    },
    PostStatus.RENDERED: {
        PostStatus.PUBLISHING,
        PostStatus.EDITING,
        PostStatus.RENDERING,
        PostStatus.ARCHIVED,
    },
    PostStatus.PUBLISHING: {
        PostStatus.PUBLISHED,
        PostStatus.FAILED,
        PostStatus.PUBLISHING,
        PostStatus.ARCHIVED,
    },
    PostStatus.PUBLISHED: {PostStatus.ARCHIVED},
    PostStatus.FAILED: {
        PostStatus.RESOLVING,
        PostStatus.PUBLISHING,
        PostStatus.RENDERING,
        PostStatus.DOWNLOADING,
        PostStatus.ARCHIVED,
    },
    PostStatus.ARCHIVED: set(),
}


def test_the_transition_table_is_the_expected_snapshot() -> None:
    assert {key: set(value) for key, value in ALLOWED_TRANSITIONS.items()} == EXPECTED_TRANSITIONS


def test_a_rendered_post_can_be_rendered_again_but_never_fails() -> None:
    assert PostStatus.RENDERING in ALLOWED_TRANSITIONS[PostStatus.RENDERED]
    assert PostStatus.FAILED not in ALLOWED_TRANSITIONS[PostStatus.RENDERED]


def test_every_status_has_a_ukrainian_label() -> None:
    assert set(STATUS_LABELS) == set(PostStatus)
    assert all(label and not label.isascii() for label in STATUS_LABELS.values())


@pytest.mark.parametrize(
    "current", [PostStatus.RESOLVED, PostStatus.RENDERED, PostStatus.PUBLISHED]
)
def test_a_ready_or_published_post_is_editable(current: PostStatus) -> None:
    verdict = editability_of(current, None)

    assert verdict.editable is True
    assert verdict.reason is None


@pytest.mark.parametrize(
    "stage",
    [
        PostStatus.DOWNLOADING,
        PostStatus.DOWNLOADED,
        PostStatus.EDITING,
        PostStatus.RENDERING,
        PostStatus.PUBLISHING,
    ],
)
def test_a_post_that_failed_after_the_draft_is_editable(stage: PostStatus) -> None:
    assert editability_of(PostStatus.FAILED, stage) == status.Editability(True, None)


@pytest.mark.parametrize("stage", [PostStatus.RESOLVING, None])
def test_a_post_that_failed_while_resolving_or_at_an_unknown_stage_is_not(
    stage: PostStatus | None,
) -> None:
    verdict = editability_of(PostStatus.FAILED, stage)

    assert verdict.editable is False
    assert verdict.reason is not None
    assert "/retry" in verdict.reason


@pytest.mark.parametrize(
    "current",
    [
        PostStatus.DRAFT,
        PostStatus.RESOLVING,
        PostStatus.DOWNLOADING,
        PostStatus.DOWNLOADED,
        PostStatus.EDITING,
        PostStatus.RENDERING,
        PostStatus.PUBLISHING,
        PostStatus.ARCHIVED,
    ],
)
def test_every_other_state_is_read_only_with_a_sentence_that_names_no_state(
    current: PostStatus,
) -> None:
    verdict = editability_of(current, None)

    assert verdict.editable is False
    assert verdict.reason
    assert not any(state.value in verdict.reason.lower() for state in PostStatus)


def test_an_archived_post_gets_its_own_sentence() -> None:
    assert editability_of(PostStatus.ARCHIVED, None).reason == status.ARCHIVED_REASON


def test_a_previewed_video_can_have_its_fragment_chosen() -> None:
    assert choosability_of(PostStatus.RENDERED, None) == status.Choosability(True, None)


@pytest.mark.parametrize("stage", [PostStatus.DOWNLOADED, PostStatus.RENDERING])
def test_a_render_that_failed_can_have_its_fragment_chosen(stage: PostStatus) -> None:
    assert choosability_of(PostStatus.FAILED, stage) == status.Choosability(True, None)


@pytest.mark.parametrize(
    "stage",
    [PostStatus.RESOLVING, PostStatus.DOWNLOADING, PostStatus.PUBLISHING, None],
)
def test_a_failure_at_another_stage_cannot_have_its_fragment_chosen(
    stage: PostStatus | None,
) -> None:
    verdict = choosability_of(PostStatus.FAILED, stage)

    assert verdict == status.Choosability(False, status.FRAGMENT_FAILED_REASON)
    assert "/retry" in status.FRAGMENT_FAILED_REASON


@pytest.mark.parametrize(
    "current",
    [state for state in PostStatus if state not in {PostStatus.RENDERED, PostStatus.FAILED}],
)
def test_every_other_state_refuses_the_choice_with_a_sentence_that_names_no_state(
    current: PostStatus,
) -> None:
    verdict = choosability_of(current, None)

    assert verdict.choosable is False
    assert verdict.reason
    assert not any(state.value in verdict.reason.lower() for state in PostStatus)


def test_a_published_post_and_a_running_render_get_sentences_of_their_own() -> None:
    published = choosability_of(PostStatus.PUBLISHED, None).reason
    rendering = choosability_of(PostStatus.RENDERING, None).reason

    assert published == status.FRAGMENT_PUBLISHED_REASON
    assert rendering == status.MEDIA_IN_PROGRESS_REASON
    assert published != rendering


def test_no_state_is_left_without_a_verdict() -> None:
    for current in PostStatus:
        verdict = editability_of(current, PostStatus.RENDERING)
        assert verdict.editable == (verdict.reason is None)
