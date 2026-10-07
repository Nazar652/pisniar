"""The static facts the editor needs: which hosts a platform may link to, the release types,
and the language and genre suggestions. Taken from the same tables the server validates with.
"""

from __future__ import annotations

from pisniar.db.models import DEFAULT_LANGUAGES, MANUAL_FIELD_NAMES
from pisniar.post.caption import MEDIA_CAPTION_LIMIT, RELEASE_TYPE_NAMES, platform_display_name
from pisniar.post.hashtags import default_hashtag
from pisniar.resolve.genres import DEEZER_GENRE_NAMES
from pisniar.resolve.link_policy import ALLOWED_HOST_SUFFIXES, ALLOWED_HOSTS, CLASSIFIED_PLATFORMS
from pisniar.webapp.schemas import GenreHint, MetaResponse, PlatformRule, ReleaseTypeOption

LANGUAGE_HINTS = (*DEFAULT_LANGUAGES, "англійська")


def build_meta() -> MetaResponse:
    return MetaResponse(
        platforms=[
            PlatformRule(
                key=platform,
                display_name=platform_display_name(platform),
                hosts=sorted(ALLOWED_HOSTS[platform]),
                host_suffixes=list(ALLOWED_HOST_SUFFIXES.get(platform, ())),
                requires_track_link=platform in CLASSIFIED_PLATFORMS,
            )
            for platform in sorted(ALLOWED_HOSTS)
        ],
        release_types=[
            ReleaseTypeOption(value=value, label=label)
            for value, label in RELEASE_TYPE_NAMES.items()
        ],
        languages=list(LANGUAGE_HINTS),
        genres=[
            GenreHint(name=ukrainian_name, default_hashtag=default_hashtag(ukrainian_name))
            for _, ukrainian_name in sorted(DEEZER_GENRE_NAMES.values(), key=lambda pair: pair[1])
        ],
        manual_field_names=list(MANUAL_FIELD_NAMES),
        caption_limit=MEDIA_CAPTION_LIMIT,
    )
