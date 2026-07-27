#!/usr/bin/env bash
# Cut a release: bump the single project version, tag, push.
#
#   scripts/release.sh <major|minor|patch|X.Y.Z> [--no-verify] [--no-push]
#
# The VERSION file at the repo root is the source of truth; both package
# pyprojects are kept in lockstep and the annotated tag vX.Y.Z is what the
# installer and `impi update` discover (sort -V over v* tags). SemVer 0.x:
# MINOR = features/breaking, PATCH = fixes.

set -euo pipefail

REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_DIR"

BUMP=${1:-}
VERIFY=1
PUSH=1
shift || true
while [ $# -gt 0 ]; do
    case "$1" in
        --no-verify) VERIFY=0; shift ;;
        --no-push) PUSH=0; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
[ -n "$BUMP" ] || { echo "usage: release.sh <major|minor|patch|X.Y.Z> [--no-verify] [--no-push]" >&2; exit 2; }

[ -z "$(git status --porcelain)" ] || { echo "working tree is dirty" >&2; exit 1; }
BRANCH=$(git rev-parse --abbrev-ref HEAD)
[ "$BRANCH" = main ] || { echo "releases are cut from main (on: $BRANCH)" >&2; exit 1; }

CURRENT=$(cat VERSION)
IFS=. read -r MAJ MIN PAT <<EOF
$CURRENT
EOF
case "$BUMP" in
    major) NEW="$((MAJ + 1)).0.0" ;;
    minor) NEW="$MAJ.$((MIN + 1)).0" ;;
    patch) NEW="$MAJ.$MIN.$((PAT + 1))" ;;
    [0-9]*.[0-9]*.[0-9]*) NEW=$BUMP ;;
    *) echo "bad bump: $BUMP" >&2; exit 2 ;;
esac
git rev-parse -q --verify "refs/tags/v$NEW" >/dev/null && { echo "tag v$NEW already exists" >&2; exit 1; }

if [ "$VERIFY" = 1 ]; then
    make lint
    make test
fi

printf '%s\n' "$NEW" > VERSION
for f in packages/crucible/pyproject.toml packages/impi/pyproject.toml; do
    python3 - "$f" "$NEW" <<'EOF'
import re, sys
path, version = sys.argv[1], sys.argv[2]
text = open(path).read()
text, n = re.subn(r'(?m)^version = "[^"]+"$', f'version = "{version}"', text, count=1)
assert n == 1, f"no version line in {path}"
open(path, "w").write(text)
EOF
done
uv lock --quiet

git add VERSION packages/crucible/pyproject.toml packages/impi/pyproject.toml uv.lock
git commit -m "release v$NEW"
git tag -a "v$NEW" -m "impi v$NEW"
echo "tagged v$NEW"
if [ "$PUSH" = 1 ]; then
    git push --follow-tags
    echo "pushed"
else
    echo "not pushed (--no-push); run: git push --follow-tags"
fi
