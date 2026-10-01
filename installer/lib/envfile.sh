# env_set / env_get for .env-style files, bash 3.2 + BSD-tools compatible.
# env_set rewrites via a temp file and then TRUNCATES the target in place
# (cat > file), never mv: the file's inode survives, so a container that has
# the config directory (or even the file) mounted keeps seeing updates. Also
# sidesteps macOS `sed -i` incompatibilities.

# env_set KEY VALUE FILE — values with whitespace or shell-special characters
# are double-quoted (escaped), so the file stays BOTH compose-env-file parseable
# and bash-sourceable (the wrapper sources compose.env).
env_set() {
    local key=$1 value=$2 file=$3 tmp
    case "$value" in
        *[![:alnum:]_@%+=:,./-]*)
            value=$(printf '%s' "$value" | sed -e 's/[\\"$`]/\\&/g')
            value="\"$value\""
            ;;
    esac
    if [ ! -e "$file" ]; then
        : >"$file"
        chmod 600 "$file"
    fi
    tmp="${file}.tmp.$$"
    # ENVIRON, not -v: -v assignments undergo awk escape processing and would
    # mangle backslashes in quoted values.
    ENV_SET_KEY="$key" ENV_SET_VALUE="$value" awk '
        BEGIN { done = 0; key = ENVIRON["ENV_SET_KEY"]; value = ENVIRON["ENV_SET_VALUE"] }
        index($0, key "=") == 1 { if (!done) { print key "=" value; done = 1 }; next }
        { print }
        END { if (!done) print key "=" value }
    ' "$file" >"$tmp"
    cat "$tmp" >"$file"
    rm -f "$tmp"
}

# env_get KEY FILE -> prints the value (empty if absent); strips one layer of
# surrounding single/double quotes.
env_get() {
    local key=$1 file=$2 line value
    [ -e "$file" ] || return 0
    line=$(grep "^${key}=" "$file" | tail -n 1) || true
    [ -z "$line" ] && return 0
    value=${line#*=}
    case "$value" in
        \'*\') value=${value#\'}; value=${value%\'} ;;
        \"*\") value=${value#\"}; value=${value%\"} ;;
    esac
    printf '%s\n' "$value"
}

# write_interactivity_env GATEWAY WIDGETS MM_MODE PUBLIC_URL ENV_FILE — the
# engine's interactivity keys. Two decisions that used to share one condition,
# kept apart here: whether interactivity is ON (the widgets answer), and
# whether the click RECEIVER needs a port and a public URL (only Mattermost
# calls back over HTTP; Slack delivers clicks on its own socket).
#
# So Slack with widgets gets neither key: the engine's default is on, and its
# gateway builds no receiver. Writing INTEGRATIONS_ENABLED=false there — which
# is what the fused condition did — left every button dead on a Slack install.
write_interactivity_env() {
    local gateway=$1 widgets=$2 mm_mode=$3 public_url=$4 env_file=$5
    if [ "$widgets" != yes ]; then
        env_set INTEGRATIONS_ENABLED false "$env_file"
        return 0
    fi
    [ "$gateway" = mattermost ] || return 0
    env_set INTEGRATIONS_PORT 8423 "$env_file"
    if [ "$mm_mode" = codeploy ]; then
        env_set INTEGRATIONS_PUBLIC_URL "http://impi:8423" "$env_file"
    else
        env_set INTEGRATIONS_PUBLIC_URL "$public_url" "$env_file"
    fi
}

# write_ward_env GATEWAY APPROVERS MM_URL ENV_FILE — the broker's own env file,
# which the engine does not read. What the broker needs from the chat platform
# differs by platform: on Mattermost a server and a bot token, on Slack a bot
# token and the app-level token the socket is opened with (no receiver, no
# callback URL, no command token — clicks and the slash command come down the
# socket). The token keys are written EMPTY on purpose: the broker posts as its
# own account, and until that token is here it can decide nothing — every
# request needing a human is refused. An empty key in the file is the reminder.
write_ward_env() {
    local gateway=$1 approvers=$2 mm_url=$3 env_file=$4
    env_set WARD_APPROVERS "$approvers" "$env_file"
    if [ "$gateway" = slack ]; then
        env_set WARD_GATEWAY slack "$env_file"
        env_set WARD_SLACK_BOT_TOKEN "" "$env_file"
        env_set WARD_SLACK_APP_TOKEN "" "$env_file"
        return 0
    fi
    env_set WARD_MATTERMOST_URL "$mm_url" "$env_file"
    env_set WARD_MATTERMOST_TOKEN "" "$env_file"
}
