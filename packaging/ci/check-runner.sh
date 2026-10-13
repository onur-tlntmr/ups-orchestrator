#!/usr/bin/env bash
# Preflight for an act_runner quadlet on ci-01, run as root before (re)starting it. One script
# for every runner on the VM (tella-runner, ups-ci-runner): everything runner-specific is read
# from the runner's own quadlet and config.yaml. Installed as /root/srv/check-runner.sh.
#
# Usage: check-runner.sh <runner>        e.g. check-runner.sh tella-runner
#
# Checks: the quadlet parses; every host path it mounts or reads exists; the Podman socket;
# every label's image is local or pullable; every `-v` in container.options is listed in
# valid_volumes (act_runner silently drops one that is not) and exists on the host; and the
# build lock shared by all runners (/run/ci-locks, mounted as /locks).
#
# Exit status: 0 when nothing FAILed (WARN lines do not count).
set -u

runner="${1:-}"
[[ -n "$runner" ]] || { sed -n '2,12p' "$0"; exit 2; }

# Overridable for testing only; the defaults are ci-01's layout.
UNIT_DIR="${UNIT_DIR:-/etc/containers/systemd}"
GENERATOR="${GENERATOR:-/usr/lib/systemd/system-generators/podman-system-generator}"
PODMAN_SOCK="${PODMAN_SOCK:-/run/podman/podman.sock}"
LOCK_DIR="${LOCK_DIR:-/run/ci-locks}"
LOCK_TMPFILES="${LOCK_TMPFILES:-/etc/tmpfiles.d/ci-locks.conf}"

unit="$UNIT_DIR/$runner.container"
fail=0
ok()   { echo "OK   $*"; }
warn() { echo "WARN $*"; }
bad()  { echo "FAIL $*"; fail=1; }

[[ -f "$unit" ]] || { bad "no quadlet at $unit"; exit 1; }

# 1. Does the quadlet generator accept the file at all?
if gen=$(QUADLET_UNIT_DIRS="$UNIT_DIR" "$GENERATOR" --dryrun 2>&1) && grep -q "$runner" <<<"$gen"; then
  ok "quadlet parse"
else
  bad "quadlet parse:"; echo "$gen"
fi

# 2. Does every host path the unit mounts/reads exist?
while IFS= read -r p; do
  [[ -e "$p" ]] && ok "$p" || bad "missing path: $p"
done < <(grep -E '^(Volume|EnvironmentFile)=' "$unit" \
         | sed -E 's/^Volume=([^:]+):.*/\1/; s/^EnvironmentFile=-?//' | grep '^/')

# 3. Socket
[[ -S "$PODMAN_SOCK" ]] && ok "podman socket" \
  || bad "no socket at $PODMAN_SOCK (systemctl enable --now podman.socket)"

cfg=$(grep -E '^Volume=.*:/config/config.yaml' "$unit" | sed -E 's/^Volume=([^:]+):.*/\1/')
if [[ ! -f "$cfg" ]]; then
  bad "no config.yaml mounted by $unit"
  echo "--- fix the FAIL lines first ---"; exit 1
fi

# Items of a YAML block list (`key:` followed by `- item` lines), comments and quotes stripped.
yaml_list() {
  awk -v key="$1" '
    $0 ~ "^[[:space:]]*" key ":" { inside = 1; next }
    inside && /^[[:space:]]*#/   { next }
    inside && /^[[:space:]]*-/   { sub(/^[[:space:]]*-[[:space:]]*/, ""); gsub(/["\047]/, ""); print; next }
    inside                       { inside = 0 }
  ' "$cfg"
}

# 4. Images behind the labels: a local one must already be built; a registry one is pulled
#    on the first job.
while IFS= read -r label; do
  image="${label#*docker://}"
  [[ "$image" == "$label" ]] && continue   # host labels have no image
  first="${image%%/*}"
  if podman image exists "$image" 2>/dev/null; then
    ok "image $image"
  elif [[ "$image" == */* && "$first" == *.* && "$first" != localhost ]]; then
    ok "image $image (pulled on first job)"
  else
    warn "image $image is not built yet (jobs with label ${label%%:*} will not start)"
  fi
done < <(yaml_list labels)

# 5. container.options mounts: listed in valid_volumes, and present on the host.
mapfile -t valid < <(yaml_list valid_volumes)
allowed() {
  local v
  for v in "${valid[@]}"; do
    # shellcheck disable=SC2053  # valid_volumes entries may be globs
    [[ "$1" == $v ]] && return 0
  done
  return 1
}
mapfile -t mounts < <(grep -vE '^[[:space:]]*#' "$cfg" | grep -oE -- '-v[[:space:]]+[^[:space:]"]+' \
                      | awk '{print $2}')
for m in "${mounts[@]}"; do
  src="${m%%:*}"
  allowed "$src" || { bad "-v $m: '$src' is not in valid_volumes, so act_runner drops it"; continue; }
  if [[ "$src" == /* ]]; then
    [[ -e "$src" ]] && ok "-v $m" || bad "-v $m: $src does not exist"
  else
    ok "-v $m (named volume)"
  fi
done

# 6. The build lock shared by every runner on the VM (see packaging/ci/README.md).
[[ -f "$LOCK_TMPFILES" ]] && ok "$LOCK_TMPFILES" \
  || bad "$LOCK_TMPFILES missing (ups-orchestrator packaging/ci/tmpfiles.d/ci-locks.conf)"
[[ -d "$LOCK_DIR" ]] && ok "$LOCK_DIR" \
  || bad "$LOCK_DIR missing (systemd-tmpfiles --create $LOCK_TMPFILES)"
printf '%s\n' "${mounts[@]}" | grep -qE "^$LOCK_DIR:/locks(:|$)" && ok "build lock mounted as /locks" \
  || bad "container.options has no '-v $LOCK_DIR:/locks' (the workflows' lock step needs it)"

[[ $fail -eq 0 ]] && echo "--- all good, (re)start $runner ---" || echo "--- fix the FAIL lines first ---"
exit $fail
