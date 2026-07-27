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
