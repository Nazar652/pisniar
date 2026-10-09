"""What an admin sees of a post's state: a Ukrainian label, and whether the fields can be
edited or the fragment chosen right now, with a sentence that never names the state itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from pisniar.db.states import PostStatus

STATUS_LABELS = {
    PostStatus.DRAFT: "Новий",
    PostStatus.RESOLVING: "Обробляється",
    PostStatus.RESOLVED: "Чернетка готова",
    PostStatus.DOWNLOADING: "Завантажується аудіо",
    PostStatus.DOWNLOADED: "Аудіо завантажено",
    PostStatus.EDITING: "Редагується",
    PostStatus.RENDERING: "Рендериться відео",
    PostStatus.RENDERED: "Відео готове",
    PostStatus.PUBLISHING: "Публікується",
    PostStatus.PUBLISHED: "Опубліковано",
    PostStatus.FAILED: "Помилка",
    PostStatus.ARCHIVED: "Архівовано",
}

# a failure at these stages leaves a draft (and maybe a preview) that can still be corrected
EDITABLE_FAILURE_STAGES = frozenset(
    {
        PostStatus.DOWNLOADING,
        PostStatus.DOWNLOADED,
        PostStatus.EDITING,
        PostStatus.RENDERING,
        PostStatus.PUBLISHING,
    }
)
EDITABLE_STATUSES = frozenset({PostStatus.RESOLVED, PostStatus.RENDERED, PostStatus.PUBLISHED})

ARCHIVED_REASON = "Пост архівовано — дій із ним немає."
NOT_RESOLVED_REASON = "Пост ще не оброблено."
RESOLVING_REASON = "Пост зараз обробляється. Зачекайте на чернетку або на помилку."
MEDIA_IN_PROGRESS_REASON = "Для цього поста зараз готується відео. Дочекайтесь прев'ю."
PUBLISHING_REASON = "Пост зараз публікується. Дочекайтесь підтвердження."
RESOLVE_FAILED_REASON = "Обробка поста завершилась помилкою. Повторіть її командою /retry."
UNKNOWN_FAILURE_REASON = (
    "Пост завершився помилкою на невідомому етапі. Повторіть його командою /retry."
)


@dataclass(frozen=True)
class Editability:
    editable: bool
    reason: str | None


FRAGMENT_PUBLISHED_REASON = "Пост уже опубліковано — відео в каналі змінити не можна."
FRAGMENT_NOT_RENDERED_REASON = (
    "Фрагмент обирають після першого прев'ю відео. Спершу натисніть «Зробити відео» в чаті."
)
FRAGMENT_FAILED_REASON = (
    "Пост завершився помилкою на етапі, де фрагмент змінити не можна. "
    "Повторіть його командою /retry."
)
# a failure at these stages leaves the audio on the volume and a video to redo
CHOOSABLE_FAILURE_STAGES = frozenset({PostStatus.DOWNLOADED, PostStatus.RENDERING})
# every status that is neither `RENDERED` nor `FAILED` has one sentence of its own
FRAGMENT_REFUSAL_REASONS = {
    PostStatus.DRAFT: NOT_RESOLVED_REASON,
    PostStatus.RESOLVING: RESOLVING_REASON,
    PostStatus.RESOLVED: FRAGMENT_NOT_RENDERED_REASON,
    PostStatus.DOWNLOADING: MEDIA_IN_PROGRESS_REASON,
    PostStatus.DOWNLOADED: MEDIA_IN_PROGRESS_REASON,
    PostStatus.EDITING: MEDIA_IN_PROGRESS_REASON,
    PostStatus.RENDERING: MEDIA_IN_PROGRESS_REASON,
    PostStatus.PUBLISHING: PUBLISHING_REASON,
    PostStatus.PUBLISHED: FRAGMENT_PUBLISHED_REASON,
    PostStatus.ARCHIVED: ARCHIVED_REASON,
}


@dataclass(frozen=True)
class Choosability:
    choosable: bool
    reason: str | None


def choosability_of(status: PostStatus, failed_from_status: PostStatus | None) -> Choosability:
    """a fragment is chosen on a previewed video or after a render that failed; the reason is a
    sentence for the admin, never the name of the state."""
    if status is PostStatus.RENDERED:
        return Choosability(True, None)
    if status is PostStatus.FAILED:
        if failed_from_status in CHOOSABLE_FAILURE_STAGES:
            return Choosability(True, None)
        return Choosability(False, FRAGMENT_FAILED_REASON)
    return Choosability(False, FRAGMENT_REFUSAL_REASONS[status])


def editability_of(status: PostStatus, failed_from_status: PostStatus | None) -> Editability:
    """the reason is a sentence for the admin, never the name of the state."""
    if status in EDITABLE_STATUSES:
        return Editability(True, None)
    if status is PostStatus.FAILED:
        if failed_from_status in EDITABLE_FAILURE_STAGES:
            return Editability(True, None)
        if failed_from_status is PostStatus.RESOLVING:
            return Editability(False, RESOLVE_FAILED_REASON)
        return Editability(False, UNKNOWN_FAILURE_REASON)
    if status is PostStatus.ARCHIVED:
        return Editability(False, ARCHIVED_REASON)
    if status is PostStatus.DRAFT:
        return Editability(False, NOT_RESOLVED_REASON)
    if status is PostStatus.RESOLVING:
        return Editability(False, RESOLVING_REASON)
    if status is PostStatus.PUBLISHING:
        return Editability(False, PUBLISHING_REASON)
    return Editability(False, MEDIA_IN_PROGRESS_REASON)
