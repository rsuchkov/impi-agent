# impi installer TUI: palette, logo, prompts, arrow-key menus, step runner.
# Sourced by main.sh / install.sh. Must stay bash-3.2 compatible (macOS):
# no associative arrays, no ${var,,}, no mapfile, integer-only read -t.
#
# Input comes from fd 3 (opened on /dev/tty), never stdin — under
# `curl ... | bash` stdin is the script itself. Answers mode (IMPI_ASSUME_YES)
# never reads at all: every prompt takes its value from an IMPI_* variable.

# --- state -------------------------------------------------------------------

# shellcheck disable=SC2034  # informational state, read by future screens
TUI_COLOR=0   # 1 = 256-color output
TUI_FANCY=0   # 1 = arrow-key menus + spinner (needs a tty and a sane TERM)
TUI_LOG=""    # step runner log file (set by main.sh)

# Palette: the impi mascot sheet — ink shadow, cream lettering, wine accent,
# a warm flame for highlights.
_C_CREAM='' _C_WINE='' _C_FLAME='' _C_DIM='' _C_BOLD='' _C_RST='' _C_REV=''

tui_init() {
    if [ -e /dev/tty ] && [ -z "${IMPI_ASSUME_YES:-}" ]; then
        exec 3</dev/tty || return 1
        TUI_FANCY=1
    fi
    [ "${TERM:-dumb}" = dumb ] && TUI_FANCY=0
    if [ -z "${NO_COLOR:-}" ] && [ -t 1 ] && [ "$(tput colors 2>/dev/null || echo 0)" -ge 256 ]; then
        TUI_COLOR=1
        _C_CREAM=$'\033[38;5;230m'
        _C_WINE=$'\033[38;5;132m'
        _C_FLAME=$'\033[38;5;179m'
        _C_DIM=$'\033[38;5;245m'
        _C_BOLD=$'\033[1m'
        _C_RST=$'\033[0m'
        _C_REV=$'\033[7m'
    elif [ -z "${NO_COLOR:-}" ] && [ -t 1 ]; then
        _C_BOLD=$'\033[1m'
        _C_RST=$'\033[0m'
        _C_REV=$'\033[7m'
    fi
    return 0
}

# --- output ------------------------------------------------------------------

say()  { printf '%s\n' "$*"; }
bold() { printf '%s%s%s\n' "$_C_BOLD" "$*" "$_C_RST"; }
dim()  { printf '%s%s%s\n' "$_C_DIM" "$*" "$_C_RST"; }
ok()   { printf '%s✔%s %s\n' "${_C_FLAME:-}" "$_C_RST" "$*"; }
bad()  { printf '%s✘%s %s\n' "${_C_WINE:-}" "$_C_RST" "$*" >&2; }
die()  { bad "$*"; exit 1; }

hr() { printf '%s%s%s\n' "$_C_DIM" '────────────────────────────────────────────────────' "$_C_RST"; }

title() {
    printf '\n%s%s%s%s\n' "$_C_BOLD" "$_C_CREAM" "$*" "$_C_RST"
    hr
}

# --- prompts -------------------------------------------------------------------
# Every prompt takes an ANSWER VARIABLE name first. If that variable is already
# set (environment or --answers file), it is used as-is — no terminal round-trip.
# In answers mode a missing value is a hard error: CI stays deterministic.

_need_tty() {
    [ "$TUI_FANCY" = 1 ] && return 0
    [ -n "${IMPI_ASSUME_YES:-}" ] && die "answers mode: variable $1 is required but unset"
    die "no terminal available for prompt ($1) — use --answers"
}

# ask VAR "Label" ["default"] — in answers mode an unset variable takes the
# default when one exists; only default-less prompts are hard errors there.
ask() {
    local _var=$1 _label=$2 _default=${3:-} _value=''
    eval "_value=\${$_var:-}"
    if [ -n "$_value" ]; then return 0; fi
    if [ -n "${IMPI_ASSUME_YES:-}" ] && [ -n "$_default" ]; then
        eval "$_var=\$_default"
        return 0
    fi
    _need_tty "$_var"
    if [ -n "$_default" ]; then
        printf '%s%s%s %s[%s]%s: ' "$_C_BOLD" "$_label" "$_C_RST" "$_C_DIM" "$_default" "$_C_RST"
    else
        printf '%s%s%s: ' "$_C_BOLD" "$_label" "$_C_RST"
    fi
    IFS= read -r -u3 _value || _value=""
    [ -z "$_value" ] && _value=$_default
    while [ -z "$_value" ]; do
        printf '%s  (a value is required)%s\n' "$_C_DIM" "$_C_RST"
        printf '%s%s%s: ' "$_C_BOLD" "$_label" "$_C_RST"
        IFS= read -r -u3 _value || _value=""
    done
    eval "$_var=\$_value"
}

# ask_opt VAR "Label" — like ask, but an empty answer is accepted (stays "").
# A pre-set variable (answers mode) is used as-is; in answers mode an unset
# optional simply stays empty instead of erroring.
ask_opt() {
    local _var=$1 _label=$2 _value=
    eval "_value=\${$_var:-}"
    if [ -n "$_value" ] || [ -n "${IMPI_ASSUME_YES:-}" ]; then return 0; fi
    [ "$TUI_FANCY" = 1 ] || return 0
    printf '%s%s%s %s[skip]%s: ' "$_C_BOLD" "$_label" "$_C_RST" "$_C_DIM" "$_C_RST"
    IFS= read -r -u3 _value || _value=""
    eval "$_var=\$_value"
}

# ask_secret VAR "Label" — no echo, no default
ask_secret() {
    local _var=$1 _label=$2 _value=
    eval "_value=\${$_var:-}"
    if [ -n "$_value" ]; then return 0; fi
    _need_tty "$_var"
    printf '%s%s%s: ' "$_C_BOLD" "$_label" "$_C_RST"
    IFS= read -rs -u3 _value || _value=""
    printf '\n'
    while [ -z "$_value" ]; do
        printf '%s  (a value is required)%s\n' "$_C_DIM" "$_C_RST"
        printf '%s%s%s: ' "$_C_BOLD" "$_label" "$_C_RST"
        IFS= read -rs -u3 _value || _value=""
        printf '\n'
    done
    eval "$_var=\$_value"
}

# confirm VAR "Question" default(y|n) -> sets VAR=yes|no; returns 0 for yes
confirm() {
    local _var=$1 _question=$2 _default=${3:-y} _value='' _hint
    eval "_value=\${$_var:-}"
    if [ -z "$_value" ] && [ -n "${IMPI_ASSUME_YES:-}" ]; then
        _value=$_default
    fi
    if [ -z "$_value" ]; then
        _need_tty "$_var"
        [ "$_default" = y ] && _hint="Y/n" || _hint="y/N"
        printf '%s%s%s %s[%s]%s ' "$_C_BOLD" "$_question" "$_C_RST" "$_C_DIM" "$_hint" "$_C_RST"
        IFS= read -r -u3 _value || _value=""
        [ -z "$_value" ] && _value=$_default
    fi
    case "$(printf '%s' "$_value" | tr '[:upper:]' '[:lower:]')" in
        y|yes|д|да) eval "$_var=yes"; return 0 ;;
        *)          eval "$_var=no";  return 1 ;;
    esac
}

# menu VAR "Title" "opt1" "opt2" ... -> sets VAR to the SELECTED INDEX (0-based).
# Arrow keys / j,k / digits; Enter selects. Falls back to a numbered prompt on
# plain terminals. Pre-set VAR (answers mode) skips the menu entirely.
menu() {
    local _var=$1 _title=$2; shift 2
    local _value='' _n=$# _i _key _seq _choice=0 _ESC=$'\033'
    eval "_value=\${$_var:-}"
    if [ -n "$_value" ]; then return 0; fi
    _need_tty "$_var"
    printf '%s%s%s\n' "$_C_BOLD" "$_title" "$_C_RST"
    if [ "$TUI_FANCY" != 1 ]; then
        _i=1
        for _opt in "$@"; do printf '  %d) %s\n' "$_i" "$_opt"; _i=$((_i + 1)); done
        while :; do
            printf 'Choice [1-%d]: ' "$_n"
            IFS= read -r -u3 _value || _value=""
            case "$_value" in
                [1-9]|[1-9][0-9]) if [ "$_value" -le "$_n" ]; then eval "$_var=$((_value - 1))"; return 0; fi ;;
            esac
        done
    fi
    # Fancy path: reverse-video cursor row, redraw in place.
    printf '%s  ↑/↓ or j/k · Enter to choose · or press its number%s\n' "$_C_DIM" "$_C_RST"
    printf '\033[?25l'  # hide cursor
    while :; do
        _i=0
        for _opt in "$@"; do
            if [ "$_i" -eq "$_choice" ]; then
                printf '  %s%s ▸ %s %s\033[K\n' "$_C_REV" "$_C_CREAM" "$_opt" "$_C_RST"
            else
                printf '    %s\033[K\n' "$_opt"
            fi
            _i=$((_i + 1))
        done
        IFS= read -rsn1 -u3 _key || _key=""
        if [ "$_key" = "$_ESC" ]; then
            # An arrow is ESC + an introducer + a final byte: CSI ("\033[A") in
            # normal mode, SS3 ("\033OA") when the terminal is in application
            # cursor mode (macOS terminals switch into it). Read the bytes ONE
            # at a time — a -n2 read is a single-shot that different bash
            # versions satisfy differently, and it can't be told apart from a
            # bare ESC keypress.
            _seq=""
            IFS= read -rsn1 -t 1 -u3 _seq || _seq=""
            case "$_seq" in
                '['|O)
                    _seq=""
                    IFS= read -rsn1 -t 1 -u3 _seq || _seq=""
                    case "$_seq" in
                        A) _choice=$(( (_choice + _n - 1) % _n )) ;;
                        B) _choice=$(( (_choice + 1) % _n )) ;;
                    esac
                    ;;
            esac
        else
            case "$_key" in
                k) _choice=$(( (_choice + _n - 1) % _n )) ;;
                j) _choice=$(( (_choice + 1) % _n )) ;;
                [1-9]) if [ "$_key" -le "$_n" ]; then _choice=$((_key - 1)); fi ;;
                "") break ;;  # Enter
            esac
        fi
        printf '\033[%dA' "$_n"
    done
    printf '\033[?25h'  # show cursor
    eval "$_var=\$_choice"
}

# --- step runner ----------------------------------------------------------------
# run_step "label" cmd args... — runs the command, appends full output to
# TUI_LOG, prints one ✔/✘ line. On failure shows the log tail and returns 1.

run_step() {
    local _label=$1; shift
    printf '%s… %s%s' "$_C_DIM" "$_label" "$_C_RST"
    if "$@" >>"${TUI_LOG:-/dev/null}" 2>&1; then
        printf '\r\033[K'; ok "$_label"
        return 0
    fi
    printf '\r\033[K'; bad "$_label"
    if [ -n "$TUI_LOG" ]; then
        dim "── last lines of $TUI_LOG ──"
        tail -n 15 "$TUI_LOG" >&2 || true
    fi
    return 1
}
