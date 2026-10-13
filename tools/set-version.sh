#!/usr/bin/env bash
# Sets the release version, then shows how to commit and tag it. Modelled on tella's.
#
# The version lives in one place: Version: in packaging/rpm/ups-orchestrator.spec. Server and
# desktop share it, the CI workflow reads it, and a v* tag must match it or the package job
# fails — so the tag is pushed after the version commit, never before.
#
# Written:
#   packaging/rpm/ups-orchestrator.spec   Version:, Release: back to 1, and a dated %changelog
#                                         entry to fill in (author taken from the newest entry)
#
# Usage:
#   tools/set-version.sh 0.2.0
#   tools/set-version.sh 0.2.0 --no-changelog   # skip the %changelog entry
#   tools/set-version.sh --show                 # print the current version and exit
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SPEC="packaging/rpm/ups-orchestrator.spec"

NEW=""
CHANGELOG=1
SHOW=0
for arg in "$@"; do
  case "$arg" in
    --no-changelog) CHANGELOG=0 ;;
    --show)         SHOW=1 ;;
    -h|--help)      sed -n '2,15p' "$0"; exit 0 ;;
    -*) echo "unknown argument: $arg" >&2; exit 2 ;;
    *)
      [[ -z "$NEW" ]] || { echo "give a single version (second one: $arg)" >&2; exit 2; }
      NEW="$arg"
      ;;
  esac
done

read_version() { awk '/^Version:/{print $2; exit}' "$SPEC"; }
read_release() { awk '/^Release:/{sub(/%.*/, "", $2); print $2; exit}' "$SPEC"; }

if [[ "$SHOW" == 1 ]]; then
  printf '  %s  %s-%s\n' "$SPEC" "$(read_version)" "$(read_release)"
  exit 0
fi

[[ -n "$NEW" ]] || { echo "no version given (e.g. tools/set-version.sh 0.2.0)" >&2; exit 2; }
# Plain X.Y.Z: the tag (v$NEW), rpm's Version: and the registry's retention ordering all
# depend on that shape.
[[ "$NEW" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "the version must look like X.Y.Z (given: $NEW)" >&2; exit 2; }
# A tag that already exists would either be refused by the push or, if forced, rebuild a
# published version with different contents.
if git rev-parse -q --verify "refs/tags/v$NEW" >/dev/null; then
  echo "tag v$NEW already exists; pick the next version" >&2
  exit 1
fi

OLD="$(read_version)"
[[ -n "$OLD" ]] || { echo "could not read Version: from $SPEC" >&2; exit 1; }

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
log "$OLD → $NEW"

# Anchored so each edit can only match the one line it means to.
sed -i -E "s|^Version:([[:space:]]+).*|Version:\1$NEW|" "$SPEC"
sed -i -E "s|^Release:([[:space:]]+).*|Release:\11%{?dist}|" "$SPEC"

if [[ "$CHANGELOG" == 1 ]]; then
  # rpmbuild rejects a malformed date, so it is generated rather than typed. The author is the
  # newest entry's, so the changelog keeps one identity whoever runs this.
  AUTHOR="$(awk '/^%changelog$/{getline; sub(/^\* [A-Za-z]+ [A-Za-z]+ [0-9]+ [0-9]+ /, ""); sub(/ - [^ ]+$/, ""); print; exit}' "$SPEC")"
  [[ -n "$AUTHOR" ]] || { echo "could not read the author of the newest %changelog entry" >&2; exit 1; }
  DATE="$(LC_ALL=C date '+%a %b %d %Y')"
  ENTRY="* $DATE $AUTHOR - $NEW-1\n- TODO: describe this release.\n"
  sed -i "0,/^%changelog$/s|^%changelog$|%changelog\n$ENTRY|" "$SPEC"
fi

log "Version written"
printf '  %s  %s-%s\n' "$SPEC" "$(read_version)" "$(read_release)"

step=1
next() { echo "  $step. $*"; step=$((step + 1)); }
echo
echo "Next:"
[[ "$CHANGELOG" == 1 ]] && next "fill in the %changelog entry in $SPEC (the TODO line)"
next "python3 -m pytest -q tests/"
next "git commit -am \"chore: bump version to $NEW\""
next "git push"
next "git tag -a v$NEW -m \"ups-orchestrator $NEW\""
next "git push origin v$NEW        # ← builds and publishes both RPMs"
echo
echo "Tag AFTER the version commit: CI asserts that the tag matches Version: in the spec."
