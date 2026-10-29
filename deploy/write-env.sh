#!/bin/sh
# Writes .env and .env.tailscale from the process environment (the deploy job's secrets).
# Run from the checkout root. Output names keys only, never values.
set -eu
umask 077

required_keys="BOT_TOKEN ADMIN_IDS ALERT_CHAT_ID CHANNEL_ID SPOTIFY_CLIENT_ID SPOTIFY_CLIENT_SECRET REDIS_PASSWORD POSTGRES_PASSWORD"
optional_keys="WEBAPP_PUBLIC_URL MUSICBRAINZ_CONTACT LOG_LEVEL ITUNES_COUNTRY SOURCE_FETCH_TTL_SECONDS MEDIA_MIN_FREE_BYTES DOWNLOAD_TASK_SOFT_TIME_LIMIT_SECONDS DOWNLOAD_TASK_TIME_LIMIT_SECONDS RENDER_TASK_SOFT_TIME_LIMIT_SECONDS RENDER_TASK_TIME_LIMIT_SECONDS PLAIN_LYRICS_TASK_SOFT_TIME_LIMIT_SECONDS PLAIN_LYRICS_TASK_TIME_LIMIT_SECONDS"

newline=$(printf '\n_')
newline=${newline%_}
carriage_return=$(printf '\r_')
carriage_return=${carriage_return%_}

rm -f .env.new .env.tailscale.new
trap 'rm -f .env.new .env.tailscale.new' EXIT

# a value that could end the quote or the line is refused; the message carries the key only.
append_line() {
    target_file=$1
    key=$2
    value=$3

    case $value in
        *"'"* | *"$newline"* | *"$carriage_return"*)
            echo "write-env: the value of $key contains a quote or a line break" >&2
            exit 1
            ;;
    esac

    printf "%s='%s'\n" "$key" "$value" >>"$target_file"
}

: >.env.new
: >.env.tailscale.new

printf '%s\n' "ENVIRONMENT=docker" "LOG_FORMAT=json" >>.env.new

for key in $required_keys; do
    value=$(printenv "$key" || true)
    if [ -z "$value" ]; then
        echo "write-env: $key is not set" >&2
        exit 1
    fi
    append_line .env.new "$key" "$value"
done

for key in $optional_keys; do
    value=$(printenv "$key" || true)
    if [ -n "$value" ]; then
        append_line .env.new "$key" "$value"
    fi
done

value=$(printenv TS_AUTHKEY || true)
if [ -n "$value" ]; then
    append_line .env.tailscale.new TS_AUTHKEY "$value"
fi

mv -f .env.new .env
mv -f .env.tailscale.new .env.tailscale
