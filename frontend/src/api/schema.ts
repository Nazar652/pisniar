// generated from openapi.json by `make api-types`, do not edit
export interface paths {
    "/api/me": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Read Me */
        get: operations["read_me_api_me_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/meta": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Read Meta */
        get: operations["read_meta_api_meta_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/posts": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Posts */
        get: operations["list_posts_api_posts_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/posts/{post_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Read Post */
        get: operations["read_post_api_posts__post_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/posts/{post_id}/apply": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Apply Post Caption */
        post: operations["apply_post_caption_api_posts__post_id__apply_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/posts/{post_id}/archive": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Archive Published Post */
        post: operations["archive_published_post_api_posts__post_id__archive_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/posts/{post_id}/fields": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        /** Save Post Fields */
        put: operations["save_post_fields_api_posts__post_id__fields_put"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/posts/{post_id}/fragment": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Read Fragment */
        get: operations["read_fragment_api_posts__post_id__fragment_get"];
        put?: never;
        /** Start Fragment Render */
        post: operations["start_fragment_render_api_posts__post_id__fragment_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/posts/{post_id}/fragment/audio": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Start Audio Preparation */
        post: operations["start_audio_preparation_api_posts__post_id__fragment_audio_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/posts/{post_id}/fragment/cancel": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Cancel Audio Preparation */
        post: operations["cancel_audio_preparation_api_posts__post_id__fragment_cancel_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/posts/{post_id}/preview": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Preview Post Fields */
        post: operations["preview_post_fields_api_posts__post_id__preview_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/posts/{post_id}/publish-cover": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Publish Cover */
        post: operations["publish_cover_api_posts__post_id__publish_cover_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/session": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Create Session */
        post: operations["create_session_api_session_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
}
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
        /** ApiError */
        ApiError: {
            /** Code */
            code: string;
            /** Message */
            message: string;
        };
        /**
         * ApplyRequest
         * @description the hash of the caption the editor showed: the caption is rebuilt and compared before
         *     anything is sent.
         */
        ApplyRequest: {
            /** Expected Caption Sha256 */
            expected_caption_sha256: string;
        };
        /** ApplyResponse */
        ApplyResponse: {
            /** Caption Sha256 */
            caption_sha256: string;
            /** Other Messages Failed */
            other_messages_failed: string[];
            /**
             * Result
             * @enum {string}
             */
            result: "edited" | "not_modified" | "stored_only";
        };
        /** ArchiveResponse */
        ArchiveResponse: {
            /** Post Id */
            post_id: number;
            /** Status Label */
            status_label: string;
        };
        /** ArtistDetail */
        ArtistDetail: {
            /** Default Hashtag */
            default_hashtag: string | null;
            /** Hashtag */
            hashtag: string | null;
            /** Name */
            name: string;
            /** Spotify Id */
            spotify_id: string | null;
        };
        /** ArtistInput */
        ArtistInput: {
            /** Hashtag */
            hashtag?: string | null;
            /** Name */
            name: string;
        };
        /** AudioPreparationCancelled */
        AudioPreparationCancelled: {
            /** Post Id */
            post_id: number;
            /** Status Label */
            status_label: string;
        };
        /** AudioPreparationStarted */
        AudioPreparationStarted: {
            /** Post Id */
            post_id: number;
            /** Status Label */
            status_label: string;
        };
        /** CancelPreparationRequest */
        CancelPreparationRequest: {
            /**
             * Expected Post Updated At
             * Format: date-time
             */
            expected_post_updated_at: string;
        };
        /** CaptionSegmentView */
        CaptionSegmentView: {
            /** Href */
            href: string | null;
            /**
             * Kind
             * @enum {string}
             */
            kind: "text" | "link" | "line_break";
            /** Text */
            text: string | null;
        };
        /** CaptionView */
        CaptionView: {
            /** Dropped Genres */
            dropped_genres: string[];
            /** Dropped Platforms */
            dropped_platforms: string[];
            /** Limit */
            limit: number;
            /** Over Limit */
            over_limit: boolean;
            /** Release Title Dropped */
            release_title_dropped: boolean;
            /** Segments */
            segments: components["schemas"]["CaptionSegmentView"][];
            /** Sha256 */
            sha256: string;
            /** Visible Length */
            visible_length: number;
            /** Warnings */
            warnings: string[];
        };
        /**
         * CoverPublicationAvailability
         * @description whether the cover can be published without a video, from the status and the preview.
         */
        CoverPublicationAvailability: {
            /** Allowed */
            allowed: boolean;
            /** Reason */
            reason: string | null;
        };
        /** CoverPublicationRequest */
        CoverPublicationRequest: {
            /**
             * Expected Post Updated At
             * Format: date-time
             */
            expected_post_updated_at: string;
        };
        /** CoverPublicationStarted */
        CoverPublicationStarted: {
            /** Post Id */
            post_id: number;
            /** Status Label */
            status_label: string;
        };
        /** FieldError */
        FieldError: {
            /** Field */
            field: string;
            /** Message */
            message: string;
        };
        /**
         * FieldsInput
         * @description the full set of editable fields: what is missing from it is removed, not left alone.
         */
        FieldsInput: {
            /** Album Link */
            album_link?: string | null;
            /** Artists */
            artists: components["schemas"]["ArtistInput"][];
            /** Genres */
            genres: components["schemas"]["GenreInput"][];
            /** Languages */
            languages: string[];
            /** Platform Links */
            platform_links: {
                [key: string]: string;
            };
            /** Release Title */
            release_title?: string | null;
            /** Release Type */
            release_type?: string | null;
            /** Revert To Automatic */
            revert_to_automatic?: string[];
            /** Title */
            title: string;
            /** Year */
            year?: number | null;
        };
        /** FieldsInvalid */
        FieldsInvalid: {
            /** Code */
            code: string;
            /** Errors */
            errors?: components["schemas"]["FieldError"][];
            /** Message */
            message: string;
        };
        /** FieldsUpdate */
        FieldsUpdate: {
            /** Album Link */
            album_link?: string | null;
            /** Artists */
            artists: components["schemas"]["ArtistInput"][];
            /**
             * Expected Post Updated At
             * Format: date-time
             */
            expected_post_updated_at: string;
            /**
             * Expected Track Updated At
             * Format: date-time
             */
            expected_track_updated_at: string;
            /** Genres */
            genres: components["schemas"]["GenreInput"][];
            /** Languages */
            languages: string[];
            /** Platform Links */
            platform_links: {
                [key: string]: string;
            };
            /** Release Title */
            release_title?: string | null;
            /** Release Type */
            release_type?: string | null;
            /** Revert To Automatic */
            revert_to_automatic?: string[];
            /** Title */
            title: string;
            /** Year */
            year?: number | null;
        };
        /**
         * FragmentAvailability
         * @description whether the fragment can be chosen now, or the audio can be prepared for it, judged from
         *     the status alone; at most one of the first three flags is true.
         */
        FragmentAvailability: {
            /** Cancellable */
            cancellable: boolean;
            /** Choosable */
            choosable: boolean;
            /** End Ms */
            end_ms: number | null;
            /** Preparable */
            preparable: boolean;
            /** Preparing */
            preparing: boolean;
            /** Reason */
            reason: string | null;
            /** Start Ms */
            start_ms: number | null;
        };
        /** FragmentPeaks */
        FragmentPeaks: {
            /** Peaks Per Second */
            peaks_per_second: number;
            /** Values */
            values: number[];
        };
        /** FragmentRenderStarted */
        FragmentRenderStarted: {
            /** End Ms */
            end_ms: number;
            /** Post Id */
            post_id: number;
            /** Start Ms */
            start_ms: number;
            /** Status Label */
            status_label: string;
        };
        /**
         * FragmentRequest
         * @description the bounds the admin chose, in whole ms from the start of the full track, and the
         *     version of the post the editor was opened with.
         */
        FragmentRequest: {
            /** End Ms */
            end_ms: number;
            /**
             * Expected Post Updated At
             * Format: date-time
             */
            expected_post_updated_at: string;
            /** Start Ms */
            start_ms: number;
        };
        /**
         * FragmentView
         * @description the editor's data: the bounds of the current video, the audio and the waveform to draw.
         */
        FragmentView: {
            /** Audio Duration Ms */
            audio_duration_ms: number;
            /** Audio Url */
            audio_url: string;
            /**
             * Audio Url Expires At
             * Format: date-time
             */
            audio_url_expires_at: string;
            /** End Ms */
            end_ms: number;
            /** Max Length Ms */
            max_length_ms: number;
            peaks: components["schemas"]["FragmentPeaks"] | null;
            /**
             * Post Updated At
             * Format: date-time
             */
            post_updated_at: string;
            /** Start Ms */
            start_ms: number;
        };
        /** GenreDetail */
        GenreDetail: {
            /** Default Hashtag */
            default_hashtag: string | null;
            /** Hashtag */
            hashtag: string | null;
            /** Name */
            name: string;
            /** Source */
            source: string;
        };
        /** GenreHint */
        GenreHint: {
            /** Default Hashtag */
            default_hashtag: string | null;
            /** Name */
            name: string;
        };
        /** GenreInput */
        GenreInput: {
            /** Hashtag */
            hashtag?: string | null;
            /** Name */
            name: string;
        };
        /** MeResponse */
        MeResponse: {
            /** User Id */
            user_id: number;
        };
        /** MetaResponse */
        MetaResponse: {
            /** Caption Limit */
            caption_limit: number;
            /** Genres */
            genres: components["schemas"]["GenreHint"][];
            /** Languages */
            languages: string[];
            /** Manual Field Names */
            manual_field_names: string[];
            /** Platforms */
            platforms: components["schemas"]["PlatformRule"][];
            /** Release Types */
            release_types: components["schemas"]["ReleaseTypeOption"][];
        };
        /** PlatformLinkDetail */
        PlatformLinkDetail: {
            /** Match */
            match: string;
            /** Source */
            source: string;
            /** Url */
            url: string;
        };
        /** PlatformRule */
        PlatformRule: {
            /** Display Name */
            display_name: string;
            /** Host Suffixes */
            host_suffixes: string[];
            /** Hosts */
            hosts: string[];
            /** Key */
            key: string;
            /** Requires Track Link */
            requires_track_link: boolean;
        };
        /** PostDetail */
        PostDetail: {
            /** Album Id Spotify */
            album_id_spotify: string | null;
            /** Album Url */
            album_url: string | null;
            /** Artists */
            artists: components["schemas"]["ArtistDetail"][];
            caption: components["schemas"]["CaptionView"] | null;
            cover_publication: components["schemas"]["CoverPublicationAvailability"];
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /** Editable */
            editable: boolean;
            /**
             * Expected Post Updated At
             * Format: date-time
             */
            expected_post_updated_at: string;
            /**
             * Expected Track Updated At
             * Format: date-time
             */
            expected_track_updated_at: string;
            fragment: components["schemas"]["FragmentAvailability"];
            /** Genres */
            genres: components["schemas"]["GenreDetail"][];
            /** Has Video */
            has_video: boolean;
            /** Id */
            id: number;
            /** Languages */
            languages: string[];
            /** Manual Fields */
            manual_fields: string[];
            /** Not Editable Reason */
            not_editable_reason: string | null;
            /** Other Post Ids */
            other_post_ids: number[];
            /** Platform Links */
            platform_links: {
                [key: string]: components["schemas"]["PlatformLinkDetail"];
            };
            /** Published At */
            published_at: string | null;
            /** Release Title */
            release_title: string | null;
            /** Release Type */
            release_type: string | null;
            /** Removed Platforms */
            removed_platforms: string[];
            /** Status Label */
            status_label: string;
            /** Stored Caption Html */
            stored_caption_html: string | null;
            /** Stored Caption Matches */
            stored_caption_matches: boolean;
            /** Title */
            title: string | null;
            /** Year */
            year: number | null;
        };
        /** PostListResponse */
        PostListResponse: {
            /** Items */
            items: components["schemas"]["PostSummary"][];
            /** Next Before Id */
            next_before_id: number | null;
        };
        /** PostSummary */
        PostSummary: {
            /** Artists */
            artists: string[];
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /** Editable */
            editable: boolean;
            /** Has Video */
            has_video: boolean;
            /** Id */
            id: number;
            /** Not Editable Reason */
            not_editable_reason: string | null;
            /** Published At */
            published_at: string | null;
            /** Status Label */
            status_label: string;
            /** Title */
            title: string | null;
        };
        /** PreviewResponse */
        PreviewResponse: {
            caption: components["schemas"]["CaptionView"];
            /** Differs From Stored */
            differs_from_stored: boolean;
            /** Stored Caption Sha256 */
            stored_caption_sha256: string | null;
        };
        /** ReleaseTypeOption */
        ReleaseTypeOption: {
            /** Label */
            label: string;
            /** Value */
            value: string;
        };
        /** SessionRequest */
        SessionRequest: {
            /** Init Data */
            init_data: string;
        };
        /** SessionResponse */
        SessionResponse: {
            /** Expires In */
            expires_in: number;
            /** Token */
            token: string;
        };
    };
    responses: never;
    parameters: never;
    requestBodies: never;
    headers: never;
    pathItems: never;
}
export type $defs = Record<string, never>;
export interface operations {
    read_me_api_me_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MeResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
        };
    };
    read_meta_api_meta_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MetaResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
        };
    };
    list_posts_api_posts_get: {
        parameters: {
            query?: {
                before_id?: number | null;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PostListResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
        };
    };
    read_post_api_posts__post_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                post_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PostDetail"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
        };
    };
    apply_post_caption_api_posts__post_id__apply_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                post_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ApplyRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApplyResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FieldsInvalid"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
        };
    };
    archive_published_post_api_posts__post_id__archive_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                post_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ArchiveResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FieldsInvalid"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
        };
    };
    save_post_fields_api_posts__post_id__fields_put: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                post_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["FieldsUpdate"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PostDetail"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FieldsInvalid"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
        };
    };
    read_fragment_api_posts__post_id__fragment_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                post_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FragmentView"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FieldsInvalid"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
        };
    };
    start_fragment_render_api_posts__post_id__fragment_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                post_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["FragmentRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FragmentRenderStarted"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FieldsInvalid"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
        };
    };
    start_audio_preparation_api_posts__post_id__fragment_audio_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                post_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AudioPreparationStarted"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FieldsInvalid"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
        };
    };
    cancel_audio_preparation_api_posts__post_id__fragment_cancel_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                post_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["CancelPreparationRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AudioPreparationCancelled"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FieldsInvalid"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
        };
    };
    preview_post_fields_api_posts__post_id__preview_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                post_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["FieldsInput"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PreviewResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FieldsInvalid"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
        };
    };
    publish_cover_api_posts__post_id__publish_cover_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                post_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["CoverPublicationRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CoverPublicationStarted"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FieldsInvalid"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
        };
    };
    create_session_api_session_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["SessionRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SessionResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiError"];
                };
            };
        };
    };
}
