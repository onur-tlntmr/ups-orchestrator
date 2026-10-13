#!/usr/bin/env bash
# Installs the ups-orchestrator Gitea Actions runner on this machine as a rootless Podman
# Quadlet: act_runner in a container, driving the user's Podman socket to start job containers.
#
# Run it as the (non-root) user the runner should live under, on the runner VM. This directory
# is self-contained — copying packaging/ci/ there is enough; the repository itself is only ever
# checked out inside job containers.
#
# Settings come from an env file (template: runner.env.example): --env-file=<path>, else
# ~/.config/ups-ci/runner.env, else runner.env next to this script.
#
# Usage:
#   ./install-runner.sh
#   ./install-runner.sh --env-file=/path/to/runner.env
#   ./install-runner.sh --no-start        # write the files, start it yourself
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$HOME/.config/ups-ci"
QUADLETS="$HOME/.config/containers/systemd"
UNIT=ups-ci-runner.service
ENV_FILE=""
START=1

for arg in "$@"; do
  case "$arg" in
    --env-file=*) ENV_FILE="${arg#*=}" ;;
    --no-start)   START=0 ;;
    -h|--help)    sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

die() { echo "$*" >&2; exit 1; }
log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

(( EUID != 0 )) || die "run this as the user the runner should live under, not root (rootless Podman)"
command -v podman >/dev/null 2>&1 || die "podman not found (sudo dnf install podman)"

# Quadlet arrived in Podman 4.4.
podman_version="$(podman version --format '{{.Client.Version}}')"
[[ "$(printf '%s\n4.4.0\n' "$podman_version" | sort -V | head -n1)" == "4.4.0" ]] \
  || die "podman $podman_version is too old for Quadlet (needs 4.4+)"

# Without lingering, the user manager — and the runner with it — stops at logout and does not
# start at boot.
if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]]; then
  die "lingering is off for $USER; enable it once with: sudo loginctl enable-linger $USER"
fi

# Job containers get --cpus/--memory limits (config.yaml); rootless Podman can only apply them
# when systemd delegates those cgroup controllers to the user, and EL delegates memory but not
# cpu by default.
controllers="/sys/fs/cgroup/user.slice/user-$UID.slice/user@$UID.service/cgroup.controllers"
for controller in cpu memory; do
  if ! grep -qw "$controller" "$controllers" 2>/dev/null; then
    die "the '$controller' cgroup controller is not delegated to $USER; enable it once with:
  sudo mkdir -p /etc/systemd/system/user@.service.d
  printf '[Service]\nDelegate=cpu cpuset io memory pids\n' | sudo tee /etc/systemd/system/user@.service.d/delegate.conf
  sudo systemctl daemon-reload   # then log out and back in (or reboot)"
  fi
done

if [[ -z "$ENV_FILE" ]]; then
  for candidate in "$STATE/runner.env" "$HERE/runner.env"; do
    [[ -f "$candidate" ]] && { ENV_FILE="$candidate"; break; }
  done
fi
[[ -n "$ENV_FILE" && -f "$ENV_FILE" ]] \
  || die "no runner.env found; cp $HERE/runner.env.example $HERE/runner.env and fill it in"

value() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n1; }
[[ -n "$(value GITEA_INSTANCE_URL)" ]] || die "GITEA_INSTANCE_URL is empty in $ENV_FILE"
if [[ -z "$(value GITEA_RUNNER_REGISTRATION_TOKEN)" ]] \
    && ! podman volume exists ups-ci-runner-data; then
  die "GITEA_RUNNER_REGISTRATION_TOKEN is empty in $ENV_FILE and the runner was never registered"
fi

log "Podman API socket"
systemctl --user enable --now podman.socket

log "Files → $STATE, $QUADLETS"
mkdir -p "$STATE" "$QUADLETS"
install -m644 "$HERE/config.yaml" "$STATE/config.yaml"
if [[ "$(realpath "$ENV_FILE")" != "$(realpath -m "$STATE/runner.env")" ]]; then
  install -m600 "$ENV_FILE" "$STATE/runner.env"
fi
chmod 600 "$STATE/runner.env"
install -m644 "$HERE/ups-ci-runner.container" "$QUADLETS/ups-ci-runner.container"
systemctl --user daemon-reload

if [[ "$START" == 1 ]]; then
  log "Start $UNIT"
  systemctl --user restart "$UNIT"
  echo "Registration happens on the first start; confirm it with:"
  echo "  journalctl --user -u $UNIT -f"
else
  echo "Files written; start it with: systemctl --user start $UNIT"
fi

echo
echo "Status : systemctl --user status $UNIT"
echo "Remove : systemctl --user stop $UNIT; rm $QUADLETS/ups-ci-runner.container;"
echo "         systemctl --user daemon-reload; podman volume rm ups-ci-runner-data"
