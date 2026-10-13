# ups-orchestrator CI — Gitea Actions on rootless Podman

[`.gitea/workflows/ci.yml`](../../.gitea/workflows/ci.yml) tests every push and, on a `v*` tag,
builds each RPM on the distro it is installed on and publishes it to the instance's RPM registry.

```text
runner VM (Rocky Linux 10, rootless Podman, lingering user)
├── podman.socket                  user's Podman API, used by the runner as "Docker"
├── ups-ci-runner.service          quadlet: gitea/act_runner, ups-ci-runner-data volume
└── job containers (siblings)
    ├── rocky-10   → rockylinux:10   test job, server RPM  → registry group el/10
    └── fedora-44  → fedora:44       desktop RPM           → registry group fedora/44
```

Jobs run in stock distro images and install what they need with `dnf` first, so there is no
builder image to maintain.

## Install the runner

On the runner VM, as the user the runner should live under (not root):

```bash
sudo dnf install podman
sudo loginctl enable-linger "$USER"     # keep the user manager running without a login
# let rootless Podman apply the job containers' --cpus/--memory limits (EL delegates no cpu)
sudo mkdir -p /etc/systemd/system/user@.service.d
printf '[Service]\nDelegate=cpu cpuset io memory pids\n' \
  | sudo tee /etc/systemd/system/user@.service.d/delegate.conf
sudo systemctl daemon-reload            # then log in again

# copy packaging/ci/ to the VM, then:
cp runner.env.example runner.env
$EDITOR runner.env                      # GITEA_INSTANCE_URL + registration token
./install-runner.sh
journalctl --user -u ups-ci-runner -f   # "runner registered successfully"
```

The registration token comes from Gitea → repo (or org/site) **Settings → Actions → Runners →
Create new runner**. It is used only on the first start; the registration is stored in the
`ups-ci-runner-data` volume.

`install-runner.sh` enables `podman.socket`, writes `~/.config/ups-ci/{config.yaml,runner.env}`
and `~/.config/containers/systemd/ups-ci-runner.container`, then starts `ups-ci-runner.service`.
Labels and job settings live in [`config.yaml`](config.yaml); re-run the script after changing it.
Each job container is capped at 4 CPUs and 3 GiB RAM with no swap; two run at once
(`capacity: 2`). A job peaks around 1.1 GiB, so a 4 GB VM is enough.

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
