#!/usr/bin/env bash
# Publishes the RPMs in --dir to the Gitea instance's RPM package registry, so the server and the
# desktop upgrade with `dnf upgrade` instead of a hand-copied file. Adapted from tella's
# packaging/ci/publish-rpm.sh.
#
# The repository group is the path clients point at, and it comes from each RPM's own dist tag
# (`…-1.el10` → `el/10`, `…-1.fc44` → `fedora/44`): a package built for one distro must not be
# served from another's repository, where its dependencies would not resolve. --group= overrides.
#
# Uploaded with ?sign=true: Gitea signs with the instance key only when asked, while the .repo
# file it generates says gpgcheck=1. A version that is already there (409) is replaced, so a
# re-run is safe. Afterwards only the newest --keep versions of each package survive.
#
# Environment:
#   GITEA_TOKEN     required; needs `package: Read and Write`
#   GITEA_API_URL   instance base URL (default: $GITHUB_SERVER_URL, set by the runner)
#   GITEA_REPO      owner/repo (default: $GITHUB_REPOSITORY); the owner is the package namespace
#   GITEA_USER      the user the token belongs to (default: $GITHUB_ACTOR, else the owner)
#
# Usage:
#   packaging/ci/publish-rpm.sh --dir=dist --version=0.1.0 [--keep=3] [--group=el/10]
#   packaging/ci/publish-rpm.sh --dir=dist --version=0.1.0 --dry-run
set -euo pipefail

DIR=""
VERSION=""
GROUP=""
KEEP=3
DRY_RUN=0
USER_NAME="${GITEA_USER:-${GITHUB_ACTOR:-}}"
REPO="${GITEA_REPO:-${GITHUB_REPOSITORY:-}}"
API="${GITEA_API_URL:-${GITHUB_SERVER_URL:-}}"

for arg in "$@"; do
  case "$arg" in
    --dir=*)     DIR="${arg#*=}" ;;
    --version=*) VERSION="${arg#*=}" ;;
    --group=*)   GROUP="${arg#*=}" ;;
    --keep=*)    KEEP="${arg#*=}" ;;
    --dry-run)   DRY_RUN=1 ;;
    -h|--help)   sed -n '2,23p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

die() { echo "$*" >&2; exit 1; }
log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

[[ -n "$DIR" && -d "$DIR" ]] || die "--dir must name a directory with RPMs (got: '$DIR')"
[[ -n "$VERSION" ]] || die "no --version given"
[[ -n "$API" ]] || die "no instance URL (GITEA_API_URL / GITHUB_SERVER_URL)"
[[ -n "$REPO" ]] || die "no owner/repo (GITEA_REPO / GITHUB_REPOSITORY)"
[[ "$KEEP" =~ ^[0-9]+$ ]] && (( KEEP >= 1 )) || die "--keep must be a number ≥ 1 (got: $KEEP)"
API="${API%/}"
# Package paths are lowercase; repository owners are not necessarily.
OWNER="$(printf '%s' "${REPO%%/*}" | tr '[:upper:]' '[:lower:]')"
[[ -n "$USER_NAME" ]] || USER_NAME="$OWNER"
command -v rpm >/dev/null 2>&1 || die "rpm not found"

# --- Collect ------------------------------------------------------------------------------------
mapfile -t rpms < <(find "$DIR" -maxdepth 1 -type f -name '*.rpm' ! -name '*.src.rpm' | LC_ALL=C sort)
(( ${#rpms[@]} > 0 )) || die "no RPM in $DIR"

names=(); nvrs=(); arches=(); groups=()
for rpm in "${rpms[@]}"; do
  # The header, not the file name, is what dnf and the delete endpoint match against.
  read -r name pkg_version pkg_release arch < <(
    rpm -qp --nosignature --queryformat '%{NAME} %{VERSION} %{RELEASE} %{ARCH}\n' "$rpm" 2>/dev/null
  ) || die "could not read the RPM header of $rpm"
  [[ "$pkg_version" == "$VERSION" ]] \
    || die "$(basename "$rpm") is version $pkg_version, but --version says $VERSION"

  group="$GROUP"
  if [[ -z "$group" ]]; then
    if [[ "$pkg_release" =~ \.fc([0-9]+)$ ]]; then
      group="fedora/${BASH_REMATCH[1]}"
    elif [[ "$pkg_release" =~ \.el([0-9]+)(_[0-9]+)?$ ]]; then
      group="el/${BASH_REMATCH[1]}"
    else
      die "cannot derive a repository group from release '$pkg_release'; pass --group="
    fi
  fi
  names+=("$name"); nvrs+=("$pkg_version-$pkg_release"); arches+=("$arch"); groups+=("$group")
done

log "Publishing to the RPM registry of $OWNER ($API)"
for i in "${!rpms[@]}"; do
  printf '  %s  →  %s/rpm/%s\n' "$(basename "${rpms[$i]}")" "$OWNER" "${groups[$i]}"
done
printf '  keep    : %s newest versions per package\n' "$KEEP"
if [[ "$DRY_RUN" == 1 ]]; then echo; echo "--dry-run: nothing uploaded or deleted"; exit 0; fi

[[ -n "${GITEA_TOKEN:-}" ]] || die "GITEA_TOKEN is empty (needs 'package: Read and Write')"
command -v jq >/dev/null 2>&1 || die "jq not found (needed for retention)"

# The token goes in a curl config file, never on the command line.
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
CFG="$TMP/curlrc"
( umask 077; printf 'user = "%s:%s"\n' "$USER_NAME" "$GITEA_TOKEN" > "$CFG" )
BODY="$TMP/body"

# req <METHOD> <URL> [curl args...] — prints the status code, leaves the body in $BODY.
req() {
  local method="$1" url="$2"; shift 2
  curl -sS --config "$CFG" -o "$BODY" -w '%{http_code}' -X "$method" "$@" "$url" \
    || die "curl failed: $method $url"
}
fail() {
  echo "$1 $2 → HTTP $3" >&2
  head -c 2000 "$BODY" >&2; echo >&2
  (( $3 == 401 || $3 == 403 )) && echo "the token was rejected or lacks 'package: Read and Write' (user: $USER_NAME)" >&2
  exit 1
}

# --- Upload -------------------------------------------------------------------------------------
for i in "${!rpms[@]}"; do
  base="$(basename "${rpms[$i]}")"
  upload="$API/api/packages/$OWNER/rpm/${groups[$i]}/upload?sign=true"
  file_path="/api/packages/$OWNER/rpm/${groups[$i]}/package/${names[$i]}/${nvrs[$i]}/${arches[$i]}"

  status="$(req PUT "$upload" --upload-file "${rpms[$i]}")"
  if [[ "$status" == "409" ]]; then
    log "$base is already published; replacing it"
    status="$(req DELETE "$API$file_path")"
    (( status < 400 )) || fail DELETE "$file_path" "$status"
    status="$(req PUT "$upload" --upload-file "${rpms[$i]}")"
  fi
  (( status < 400 )) || fail PUT "$upload" "$status"
  echo "published $base → $OWNER/rpm/${groups[$i]}"
done

# --- Retention ----------------------------------------------------------------------------------
# Highest version wins (sort -V); what was just published is always kept; anything not shaped
# like X.Y.Z-release (a hand upload) is left alone.
mapfile -t unique_names < <(printf '%s\n' "${names[@]}" | sort -u)
for name in "${unique_names[@]}"; do
  log "Retention: keeping the newest $KEEP versions of $OWNER/$name"
  # `q` is a substring filter, so the exact name is asserted.
  status="$(req GET "$API/api/v1/packages/$OWNER?type=rpm&q=$name&limit=100")"
  (( status < 400 )) || fail GET "/api/v1/packages/$OWNER" "$status"
  mapfile -t versions < <(jq -r --arg n "$name" '.[] | select(.name == $n) | .version' "$BODY" \
    | grep -E '^[0-9]+(\.[0-9]+)*-[A-Za-z0-9._+]+$' | sort -V)

  total=${#versions[@]}
  keep_from=$(( total > KEEP ? total - KEEP : 0 ))
  for i in "${!versions[@]}"; do
    v="${versions[$i]}"
    (( i >= keep_from )) && continue
    [[ " ${nvrs[*]} " == *" $v "* ]] && continue
    status="$(req DELETE "$API/api/v1/packages/$OWNER/rpm/$name/$v")"
    (( status < 400 )) || fail DELETE "/api/v1/packages/$OWNER/rpm/$name/$v" "$status"
    echo "removed $name $v"
  done
done

log "Done — a client installs and upgrades it with:"
cat <<EOF
  # /etc/yum.repos.d/ups-orchestrator.repo   (chmod 600 if it carries credentials)
  [ups-orchestrator]
  name=UPS Orchestrator
  baseurl=$API/api/packages/$OWNER/rpm/${groups[0]}
  enabled=1
  gpgcheck=1
  gpgkey=$API/api/packages/$OWNER/rpm/repository.key
  # On an instance that requires sign-in, add a token with 'package: read':
  #username=<user>
  #password=<token>

  sudo dnf install ${names[0]}      # first time
  sudo dnf upgrade ${names[0]}      # every release after that
EOF
