#!/usr/bin/env bash
set -euo pipefail

: "${CDP_PORT:=9222}"
: "${CHROME_INTERNAL_PORT:=9223}"
: "${CHROME_PROFILE_DIR:=/profile}"
: "${CHROME_WINDOW_SIZE:=1440,900}"
: "${CHROME_IDLE_TIMEOUT:=5m}"
: "${CHROME_SPOOF_UA:=1}"
: "${CHROME_EXTRA_FLAGS:=}"

USER_DATA_DIR="${CHROME_PROFILE_DIR}/user-data"
mkdir -p "${USER_DATA_DIR}"

flags=(
    --headless
    # Loopback only, on purpose: Chrome ignores --remote-debugging-address and
    # would bind loopback anyway. The relay fronts this on a routable address.
    --remote-debugging-port="${CHROME_INTERNAL_PORT}"
    --user-data-dir="${USER_DATA_DIR}"
    --window-size="${CHROME_WINDOW_SIZE}"
    --no-first-run
    --no-default-browser-check
    # Without these Chrome blocks on a D-Bus secret service that no container has.
    --password-store=basic
    --use-mock-keychain
    --disable-gpu
    # Headless Chrome throttles hidden pages, which stalls automation waits.
    --disable-background-timer-throttling
    --disable-backgrounding-occluded-windows
    --disable-renderer-backgrounding
)

# Headless Chrome reports "HeadlessChrome/<major>.0.0.0" in its User-Agent, which
# is the single loudest automation signal an IdP sees. Rebuild the same reduced
# UA with the normal product token instead of hardcoding a version that would
# drift from the installed binary on every image rebuild.
if [[ "${CHROME_SPOOF_UA}" == "1" ]]; then
    major="$(google-chrome --version | grep -oE '[0-9]+' | head -n1)"
    flags+=(--user-agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${major}.0.0.0 Safari/537.36")
fi

# Deliberately no --no-sandbox: it drops the renderer isolation that matters most
# here, since the renderer parses untrusted HTML from login pages. The container
# is instead run with the seccomp profile in deploy/seccomp/chrome.json, which
# relaxes only the four CLONE_NEW* flags that sandbox needs. See
# deploy/compose.browser.yaml.
if [[ -n "${CHROME_EXTRA_FLAGS}" ]]; then
    read -r -a extra <<<"${CHROME_EXTRA_FLAGS}"
    flags+=("${extra[@]}")
fi

# The relay owns PID 1's job. Chrome is not started here: the relay launches it
# on the first client connection and stops it once the last one has been gone
# for CHROME_IDLE_TIMEOUT, so an idle container costs the relay alone.
exec /usr/local/bin/relay \
    -listen "0.0.0.0:${CDP_PORT}" \
    -upstream "127.0.0.1:${CHROME_INTERNAL_PORT}" \
    -idle-timeout "${CHROME_IDLE_TIMEOUT}" \
    -- google-chrome "${flags[@]}" "$@"
