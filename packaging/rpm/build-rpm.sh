#!/usr/bin/env bash
# Builds the ups-orchestrator RPMs from the committed tree (git archive HEAD) into --out.
#
# --only= picks one package; the other is switched off with `--without`, which is how CI builds
# the server on EL10 and the desktop on Fedora from the same spec. Without --only both are built.
#
# Prerequisites: git, rpm-build, python3-devel, systemd-rpm-macros.
#
# Usage:
#   packaging/rpm/build-rpm.sh --out=dist                  # server + desktop
#   packaging/rpm/build-rpm.sh --out=dist --only=server
#   packaging/rpm/build-rpm.sh --out=dist --only=desktop
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SPEC="$ROOT/packaging/rpm/ups-orchestrator.spec"
OUT=""
ONLY=""
for arg in "$@"; do
  case "$arg" in
    --out=*)  OUT="${arg#*=}" ;;
    --only=*) ONLY="${arg#*=}" ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

die() { echo "$*" >&2; exit 1; }

[[ -n "$OUT" ]] || die "no --out given (e.g. --out=dist)"
command -v rpmbuild >/dev/null 2>&1 || die "rpmbuild not found (dnf install rpm-build)"
command -v git >/dev/null 2>&1 || die "git not found (needed to produce the tarball)"

switches=()
case "$ONLY" in
  "")      ;;
  server)  switches=(--without desktop) ;;
  desktop) switches=(--without server) ;;
  *) die "--only must be server or desktop (got: $ONLY)" ;;
esac

NAME="$(awk '/^Name:/{print $2; exit}' "$SPEC")"
VERSION="$(awk '/^Version:/{print $2; exit}' "$SPEC")"
[[ -n "$NAME" && -n "$VERSION" ]] || die "could not read Name/Version from the spec"

# A private rpmbuild tree, so a build never picks up leftovers from ~/rpmbuild.
TOPDIR="$(mktemp -d)"
trap 'rm -rf "$TOPDIR"' EXIT
mkdir -p "$TOPDIR"/{SOURCES,SPECS,BUILD,RPMS,SRPMS}

echo "==> Tarball ${NAME}-${VERSION}.tar.gz (HEAD)"
git -C "$ROOT" archive --format=tar.gz --prefix="${NAME}-${VERSION}/" \
  -o "$TOPDIR/SOURCES/${NAME}-${VERSION}.tar.gz" HEAD
cp "$ROOT/packaging/rpm/${NAME}.sysusers" "$TOPDIR/SOURCES/"

echo "==> rpmbuild -bb ${switches[*]:-}"
rpmbuild -bb --define "_topdir $TOPDIR" "${switches[@]}" "$SPEC"

mkdir -p "$OUT"
find "$TOPDIR/RPMS" -type f -name '*.rpm' -exec cp -t "$OUT" {} +
echo "==> Done:"
find "$OUT" -maxdepth 1 -name "${NAME}-*${VERSION}-*.rpm" -printf '  %p\n'
