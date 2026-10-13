#!/usr/bin/env bash
# Preflight for the ups-ci-runner quadlet on ci-01 (run as root): verifies everything the unit
# and its config reference before `systemctl start ups-ci-runner`. Modelled on tella-runner's.
unit=/etc/containers/systemd/ups-ci-runner.container
fail=0

# 1. Does the quadlet generator accept the file at all?
gen=$(/usr/lib/systemd/system-generators/podman-system-generator --dryrun 2>&1)
if grep -q "ups-ci-runner" <<<"$gen"; then echo "OK   quadlet parse"
else echo "FAIL quadlet parse:"; echo "$gen"; fail=1; fi

# 2. Does every host path the unit mounts/reads actually exist?
while IFS= read -r p; do
  if [ -e "$p" ]; then echo "OK   $p"
  else echo "FAIL missing path: $p"; fail=1; fi
done < <(grep -E '^(Volume|EnvironmentFile)=' "$unit" \
         | sed -E 's/^Volume=([^:]+):.*/\1/; s/^EnvironmentFile=//' | grep '^/')

# 3. Socket
[ -S /run/podman/podman.sock ] && echo "OK   podman.sock" \
  || { echo "FAIL podman.socket is off (systemctl enable --now podman.socket)"; fail=1; }

# 4. The build lock shared with tella-runner: without it the jobs fail at their first step.
[ -f /etc/tmpfiles.d/ci-locks.conf ] && echo "OK   /etc/tmpfiles.d/ci-locks.conf" \
  || { echo "FAIL /etc/tmpfiles.d/ci-locks.conf missing (packaging/ci/tmpfiles.d/)"; fail=1; }
[ -d /run/ci-locks ] && echo "OK   /run/ci-locks" \
  || { echo "FAIL /run/ci-locks missing (systemd-tmpfiles --create ci-locks.conf)"; fail=1; }
# act_runner drops a mount that valid_volumes does not list, so both must be there.
cfg=$(grep -E '^Volume=.*config.yaml' "$unit" | sed -E 's/^Volume=([^:]+):.*/\1/')
grep -qE '^[[:space:]]*-v /run/ci-locks:/locks' "$cfg" 2>/dev/null \
  && grep -qE '^[[:space:]]*- "/run/ci-locks"' "$cfg" \
  && echo "OK   /locks mount + valid_volumes" \
  || { echo "FAIL $cfg: /run/ci-locks missing from options or valid_volumes"; fail=1; }

[ $fail -eq 0 ] && echo "--- all good, start it ---" || echo "--- fix the FAIL lines first ---"
exit $fail
