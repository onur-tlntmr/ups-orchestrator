# ups-orchestrator CI — Gitea Actions on ci-01

[`.gitea/workflows/ci.yml`](../../.gitea/workflows/ci.yml) tests every push and, on a `v*` tag,
builds each RPM on the distro it is installed on and publishes it to the instance's RPM registry.

```text
ci-01 (Rocky Linux 10, rootful Podman, files under /root/srv)
├── podman.socket                  /run/podman/podman.sock, used by both runners as "Docker"
├── /run/ci-locks/build.lock       the build lock (tmpfiles.d/ci-locks.conf)
├── tella-runner.service           tella's quadlet; its jobs take the lock exclusively
└── ups-ci-runner.service          this quadlet: gitea/act_runner, ups-ci-runner-data volume
    └── job containers (siblings; 4 CPUs, 3 GiB each, two at once; take the lock shared)
        ├── rocky-10   → rockylinux:10   test job, server RPM  → registry group el/10
        └── fedora-44  → fedora:44       desktop RPM           → registry group fedora/44
```

Jobs run in stock distro images and install what they need with `dnf` first, so there is no
builder image to maintain.

## The build lock

tella and ups-orchestrator share ci-01, and a tella build (Flutter, up to 6 GB) must not run
next to ours. Each job's first step takes `/locks/build.lock` (the host's `/run/ci-locks`) with
`flock` and keeps holding it in the background until the job container is removed:

- tella's job takes it **exclusively** and waits for it at most 10 minutes; tella-runner's
  `timeout: 25m` covers that wait plus its ~8 minute build.
- Every ups-orchestrator job takes it **shared**, so the server and desktop builds still run side
  by side; they wait for it at most 45 minutes, inside this runner's 1 hour timeout.

The lock is released the moment a job ends, whether it succeeded, failed or was cancelled, and
whoever waits starts right then; the waits are limits, not delays. The holder also lets go after
the runner's job timeout (`LOCK_HOLD`), so a job container left behind by a runner restart
cannot keep the lock. A job that runs out of wait
fails with a message instead of hitting its timeout. If `/locks` is not mounted (act_runner
silently drops a mount missing from `valid_volumes`), the step fails rather than running
unlocked.

## Install the runner

`srv/` mirrors `/root/srv` on ci-01. Beside it are what every runner on the VM shares: the
lock's `tmpfiles.d/ci-locks.conf` and `check-runner.sh`, one preflight for all runners
(`check-runner.sh tella-runner`, `check-runner.sh ups-ci-runner`) that reads everything
runner-specific from the runner's quadlet and `config.yaml`. Copy `packaging/ci/` to ci-01 (no
checkout needed there; the repo is only ever checked out inside job containers), then, as root
in that copy:

```bash
# once per VM, shared with tella-runner: the lock directory and the preflight
install -m644 tmpfiles.d/ci-locks.conf /etc/tmpfiles.d/ci-locks.conf
systemd-tmpfiles --create /etc/tmpfiles.d/ci-locks.conf
install -m755 check-runner.sh /root/srv/check-runner.sh

install -Dm644 srv/conf/ups-ci-runner/config.yaml /root/srv/conf/ups-ci-runner/config.yaml
install -Dm644 srv/containers/ups-ci-runner.container /root/srv/containers/ups-ci-runner.container
install -Dm600 srv/env/ups-ci-runner.env.example /root/srv/env/ups-ci-runner.env
$EDITOR /root/srv/env/ups-ci-runner.env            # GITEA_INSTANCE_URL + registration token
cp /root/srv/containers/ups-ci-runner.container /etc/containers/systemd/   # as for tella-runner

/root/srv/check-runner.sh ups-ci-runner && systemctl daemon-reload && systemctl start ups-ci-runner
journalctl -u ups-ci-runner -f                      # "runner registered successfully"
```

The registration token comes from Gitea → repo (or org/site) **Settings → Actions → Runners →
Create new runner**. It is used only on the first start; the registration is stored in the
`ups-ci-runner-data` volume. Labels, limits and the lock mount live in
[`config.yaml`](srv/conf/ups-ci-runner/config.yaml); run the preflight and restart the runner
after changing it. The preflight FAILs on a `-v` in `container.options` that `valid_volumes` does
not list, since act_runner would silently drop that mount.

Actions must be enabled for the repository (**Settings → Actions**).

## Release

1. Bump `Version:` (and add a `%changelog` entry) in
   [`packaging/rpm/ups-orchestrator.spec`](../rpm/ups-orchestrator.spec). Server and desktop
   share the version.
2. Commit, then `git tag -a vX.Y.Z -m vX.Y.Z && git push --follow-tags`.

The tag must equal `v` + `Version:`, or the package job fails. Only a tag publishes; a manual
run (Actions → ci → Run workflow) builds both RPMs as workflow artifacts only.

### Publishing token

The publish step uses the `PACKAGE_TOKEN` repository secret if set, else the job's automatic
token. If the automatic token is refused (401/403), create a token with
`package: Read and Write` and store it as `PACKAGE_TOKEN`. Packages are signed by Gitea with the
instance key (`?sign=true`); only the newest 3 versions of each package are kept (`--keep=3` in the workflow).

## Clients

Each machine points at its own group; the `.repo` file is printed at the end of every publish:

```ini
# /etc/yum.repos.d/ups-orchestrator.repo
[ups-orchestrator]
name=UPS Orchestrator
baseurl=https://<instance>/api/packages/<owner>/rpm/el/10      # desktop: fedora/44
enabled=1
gpgcheck=1
gpgkey=https://<instance>/api/packages/<owner>/rpm/repository.key
```

The server package needs EPEL on Rocky 10 (`python3-flask`, `nut-client`):
`sudo dnf install epel-release`.

## Building locally

```bash
packaging/rpm/build-rpm.sh --out=dist                 # both packages
packaging/rpm/build-rpm.sh --out=dist --only=server   # what CI does on rocky-10
```

The tarball is `git archive HEAD`, so only committed changes are built.
