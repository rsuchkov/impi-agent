# --answers file support: KEY=VALUE lines, one prompt variable each. Loading
# validates keys against the whitelist below and exports them; with
# IMPI_ASSUME_YES=1 the TUI never touches the terminal, so a missing required
# key becomes a hard error (deterministic CI).

# Every prompt variable the installer knows. Keep in sync with main.sh.
ANSWER_KEYS="
IMPI_HOME
IMPI_RUNTIME
IMPI_GATEWAY
IMPI_MM_MODE
IMPI_MM_URL
IMPI_MM_ADMIN_TOKEN
IMPI_MM_ADMIN_USER
IMPI_MM_ADMIN_EMAIL
IMPI_MM_ADMIN_PASSWORD
IMPI_MM_TEAM
IMPI_MM_PORT
IMPI_SUPPORT
IMPI_AGENTS_DIR
IMPI_FIRST_AGENT
IMPI_FIRST_AGENT_ROLE
IMPI_FIRST_AGENT_BOT_TOKEN
IMPI_SLACK_BOT_TOKEN
IMPI_SLACK_APP_TOKEN
IMPI_WIDGETS
IMPI_PUBLIC_URL
IMPI_INTEGRATIONS_PORT
IMPI_LLM_MODE
IMPI_LLM_BASE_URL
IMPI_LLM_API_KEY
IMPI_LLM_MODEL
IMPI_DEFAULT_PROVIDER
IMPI_DEFAULT_MODEL
IMPI_CONFIRM
IMPI_ASSUME_YES
"

_answer_key_known() {
    local key
    for key in $ANSWER_KEYS; do
        [ "$key" = "$1" ] && return 0
    done
    return 1
}

# load_answers FILE — parse, validate, export.
load_answers() {
    local file=$1 line key value lineno=0
    [ -r "$file" ] || die "answers file not readable: $file"
    while IFS= read -r line || [ -n "$line" ]; do
        lineno=$((lineno + 1))
        case "$line" in
            ''|'#'*) continue ;;
        esac
        key=${line%%=*}
        value=${line#*=}
        [ "$key" = "$line" ] && die "answers file line $lineno: expected KEY=VALUE, got: $line"
        _answer_key_known "$key" || die "answers file line $lineno: unknown key $key"
        export "$key=$value"
    done <"$file"
    export IMPI_ASSUME_YES="${IMPI_ASSUME_YES:-1}"
}
