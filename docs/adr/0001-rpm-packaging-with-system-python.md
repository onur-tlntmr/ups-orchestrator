# Ship server and desktop as noarch RPMs on system Python, in an FHS layout

The server and the desktop agent used to be installed by copying the repository to
`/opt/ups-orchestrator` and creating a venv per component. We now build two RPMs from one spec:
`ups-orchestrator-server`, built and published only for EL10 (the NUT host), and
`ups-orchestrator-desktop`, built and published only for Fedora 44. Both depend on distro
`python3-*` packages instead of bundling a venv. Files go to `/usr/share`, `/etc`,
`/var/lib` and `/var/log` instead of `/opt`. Gitea Actions builds them on a rootless Podman
runner and publishes them to Gitea's RPM registry, so both machines update with `dnf upgrade`.

## Considered options

- **Bundled venv in the RPM.** Rejected. It makes the package arch- and Python-version-specific
  and pulls PyPI into the build. Every dependency exists as a distro package where each
  component runs.
- **One package for both machines, or both packages on both distros.** Rejected.
  `python3-pydbus` does not exist on EL10 (in no repository, EPEL included), and the server
  never runs on the desktop. Building each package only where it is installed avoids shipping
  RPMs that cannot resolve their dependencies.
- **Keeping `/opt`.** Rejected. `%config(noreplace)` under `/etc` is what lets `dnf upgrade`
  keep local settings, and FHS paths remove the SELinux `chcon` step `/opt` needed.

## Consequences

- The server host needs EPEL (`python3-flask`, `nut-client`).
- Moving the desktop to EL would mean replacing pydbus with `python3-gobject`'s `Gio` D-Bus API
  first.
- The server runs as a dedicated `ups-orchestrator` user. Its NUT self-shutdown goes through a
  packaged sudoers rule, and NUT's user joins that group to read `server.env`.
