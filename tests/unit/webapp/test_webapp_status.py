"""What the admin may do with a post in each state: the label, the verdict and the sentence.
The transition table is pinned too, because the verdict is read off it, never changed by it.
"""

from __future__ import annotations

import pytest

from pisniar.db.states import ALLOWED_TRANSITIONS, PostStatus
from pisniar.webapp import status
from pisniar.webapp.editing import MEDIA_IN_PROGRESS_MESSAGE
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
    "current",
    [PostStatus.RESOLVED, PostStatus.EDITING, PostStatus.RENDERED, PostStatus.PUBLISHED],
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


CHOOSABLE = status.Choosability(True, False, False, None)

AUDIO_PREPARATION_STATUSES = {
    PostStatus.EDITING,
    PostStatus.RESOLVED,
    PostStatus.DOWNLOADING,
    PostStatus.RENDERED,
    PostStatus.FAILED,
}


def test_a_previewed_video_can_have_its_fragment_chosen() -> None:
    assert choosability_of(PostStatus.RENDERED, None) == CHOOSABLE


def test_ready_audio_can_have_its_fragment_chosen_and_be_given_up() -> None:
    assert choosability_of(PostStatus.EDITING, None) == status.Choosability(True, False, False, None, cancellable=True)


@pytest.mark.parametrize("current", list(PostStatus))
@pytest.mark.parametrize("stage", [None, *PostStatus])
def test_cancellable_follows_the_cancel_refusal_rule(current: PostStatus, stage: PostStatus | None) -> None:
    refusal = status.cancellation_refusal_of(current)

    assert choosability_of(current, stage).cancellable == (refusal is None)


def test_a_post_cannot_reach_editing_from_a_draft_or_a_preview() -> None:
    assert PostStatus.EDITING not in ALLOWED_TRANSITIONS[PostStatus.RESOLVED]
    assert PostStatus.EDITING not in ALLOWED_TRANSITIONS[PostStatus.RENDERED]


def test_the_preparable_sentence_says_how_to_get_the_cover_alone() -> None:
    assert "скасувати" in status.FRAGMENT_PREPARE_REASON
    assert "вже не вийде" not in status.FRAGMENT_PREPARE_REASON


def test_ready_audio_can_be_given_up() -> None:
    assert status.cancellation_refusal_of(PostStatus.EDITING) is None


@pytest.mark.parametrize(
    ("current", "sentence"),
    [
        (PostStatus.RESOLVED, status.NOT_CANCELLABLE_REASON),
        (PostStatus.DOWNLOADING, status.NOT_CANCELLABLE_REASON),
        (PostStatus.FAILED, status.NOT_CANCELLABLE_REASON),
        (PostStatus.PUBLISHED, status.NOT_CANCELLABLE_REASON),
        (PostStatus.DOWNLOADED, status.RENDER_STARTED_REASON),
        (PostStatus.RENDERING, status.RENDER_STARTED_REASON),
        (PostStatus.RENDERED, status.RENDER_STARTED_REASON),
    ],
)
def test_the_cancel_refusal_is_a_sentence_without_a_state_name(current: PostStatus, sentence: str) -> None:
    assert status.cancellation_refusal_of(current) == sentence
    assert not any(state.value in sentence.lower() for state in PostStatus)


@pytest.mark.parametrize(
    ("current", "stage"),
    [
        (PostStatus.RESOLVED, None),
        (PostStatus.FAILED, PostStatus.DOWNLOADING),
        (PostStatus.FAILED, PostStatus.PUBLISHING),
    ],
)
def test_a_draft_without_a_preview_can_be_published_as_a_cover(current: PostStatus, stage: PostStatus | None) -> None:
    assert status.cover_publication_of(current, stage, False) == status.CoverPublication(True, None)


@pytest.mark.parametrize("current", list(PostStatus))
@pytest.mark.parametrize("stage", [None, *PostStatus])
def test_a_post_with_a_preview_is_never_published_as_a_cover(current: PostStatus, stage: PostStatus | None) -> None:
    assert status.cover_publication_of(current, stage, True).allowed is False


@pytest.mark.parametrize("current", list(PostStatus))
@pytest.mark.parametrize("stage", [None, *PostStatus])
@pytest.mark.parametrize("has_preview", [False, True])
def test_every_cover_refusal_is_a_sentence_and_allowed_means_no_reason(
    current: PostStatus,
    stage: PostStatus | None,
    has_preview: bool,
) -> None:
    verdict = status.cover_publication_of(current, stage, has_preview)

    assert (verdict.reason is None) == verdict.allowed
    if verdict.reason is not None:
        assert not any(state.value in verdict.reason.lower() for state in PostStatus)


@pytest.mark.parametrize("stage", [PostStatus.DOWNLOADED, PostStatus.RENDERING])
def test_a_failed_render_without_a_preview_is_not_published_as_a_cover(
    stage: PostStatus,
) -> None:
    verdict = status.cover_publication_of(PostStatus.FAILED, stage, False)

    assert verdict == status.CoverPublication(False, status.COVER_RENDER_FAILED_REASON)


@pytest.mark.parametrize("stage", [PostStatus.DOWNLOADED, PostStatus.RENDERING])
def test_a_failed_render_with_a_preview_points_to_retry_not_to_the_preview_button(stage: PostStatus) -> None:
    verdict = status.cover_publication_of(PostStatus.FAILED, stage, True)

    assert verdict == status.CoverPublication(False, status.COVER_RENDER_FAILED_REASON)


def test_a_failed_publication_with_a_preview_still_points_to_the_preview() -> None:
    verdict = status.cover_publication_of(PostStatus.FAILED, PostStatus.PUBLISHING, True)

    assert verdict == status.CoverPublication(False, status.COVER_HAS_VIDEO_REASON)


def test_ready_audio_points_to_the_cancel_before_a_cover_can_go() -> None:
    verdict = status.cover_publication_of(PostStatus.EDITING, None, False)

    assert verdict == status.CoverPublication(False, status.COVER_AUDIO_READY_REASON)
    assert "скасуйте" in status.COVER_AUDIO_READY_REASON


@pytest.mark.parametrize("stage", [PostStatus.DOWNLOADED, PostStatus.RENDERING])
def test_a_render_that_failed_can_have_its_fragment_chosen(stage: PostStatus) -> None:
    assert choosability_of(PostStatus.FAILED, stage) == CHOOSABLE


def test_a_draft_can_have_its_audio_prepared_with_a_warning_about_the_cover() -> None:
    verdict = choosability_of(PostStatus.RESOLVED, None)

    assert verdict == status.Choosability(False, True, False, status.FRAGMENT_PREPARE_REASON)
    assert "обкладинку" in status.FRAGMENT_PREPARE_REASON


def test_a_failed_preparation_can_be_started_again() -> None:
    verdict = choosability_of(PostStatus.FAILED, PostStatus.DOWNLOADING)

    assert verdict == status.Choosability(False, True, False, status.FRAGMENT_PREPARE_AGAIN_REASON)


def test_a_running_preparation_is_preparing_with_a_sentence() -> None:
    verdict = choosability_of(PostStatus.DOWNLOADING, None)

    assert verdict == status.Choosability(False, False, True, status.FRAGMENT_PREPARING_REASON)


@pytest.mark.parametrize("stage", [PostStatus.RESOLVING, PostStatus.PUBLISHING, None])
def test_a_failure_at_another_stage_cannot_have_its_fragment_chosen(
    stage: PostStatus | None,
) -> None:
    verdict = choosability_of(PostStatus.FAILED, stage)

    assert verdict == status.Choosability(False, False, False, status.FRAGMENT_FAILED_REASON)
    assert "/retry" in status.FRAGMENT_FAILED_REASON


@pytest.mark.parametrize("current", [state for state in PostStatus if state not in AUDIO_PREPARATION_STATUSES])
def test_every_other_state_refuses_the_choice_with_a_sentence_that_names_no_state(
    current: PostStatus,
) -> None:
    verdict = choosability_of(current, None)

    assert (verdict.choosable, verdict.preparable, verdict.preparing) == (False, False, False)
    assert verdict.reason
    assert not any(state.value in verdict.reason.lower() for state in PostStatus)


@pytest.mark.parametrize("current", list(PostStatus))
@pytest.mark.parametrize("stage", [None, *PostStatus])
def test_at_most_one_flag_is_true_and_every_sentence_names_no_state(
    current: PostStatus, stage: PostStatus | None
) -> None:
    verdict = choosability_of(current, stage)

    flags = [verdict.choosable, verdict.preparable, verdict.preparing]
    assert sum(flags) <= 1
    assert (verdict.reason is None) == verdict.choosable
    if verdict.reason is not None:
        assert not any(state.value in verdict.reason.lower() for state in PostStatus)


def test_the_in_progress_sentences_promise_no_preview_because_the_prepare_path_has_none() -> None:
    for sentence in (status.MEDIA_IN_PROGRESS_REASON, MEDIA_IN_PROGRESS_MESSAGE):
        assert "прев" not in sentence


def test_the_ready_audio_status_has_a_label_that_says_what_to_do() -> None:
    assert STATUS_LABELS[PostStatus.EDITING] == "Аудіо готове — оберіть фрагмент"
    assert PostStatus.EDITING in status.EDITABLE_STATUSES


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
