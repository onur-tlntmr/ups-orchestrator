# One source, two packages: the server goes to the NUT host (EL10), the desktop agent to the
# desktop (Fedora). CI builds each on its own distro with the other one switched off:
#   rpmbuild -bb --without desktop ups-orchestrator.spec   # server only
#   rpmbuild -bb --without server  ups-orchestrator.spec   # desktop only
%bcond server 1
%bcond desktop 1

Name:           ups-orchestrator
Version:        0.1.0
Release:        1%{?dist}
Summary:        Coordinates server and desktop shutdowns during UPS power events
License:        MIT
Source0:        %{name}-%{version}.tar.gz
Source1:        %{name}.sysusers
BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  systemd-rpm-macros

%description
UPS Orchestrator gracefully shuts down a home server and a Linux desktop during UPS power
events. The server package runs next to NUT; the desktop package runs on the desktop.

%if %{with server}
%package server
Summary:        UPS Orchestrator server (runs next to NUT)
Requires:       python3
Requires:       python3-flask
Requires:       python3-requests
Requires:       python3-pyyaml
# Before %pre: the sysusers file adds NUT's user to the ups-orchestrator group.
Requires(pre):  nut-client
Requires:       nut-client
Requires:       curl
Requires:       iproute
Requires:       sudo
Requires:       tzdata
# Only for a wol_relay in ups_config.yml
Recommends:     openssh-clients
%{?systemd_requires}
%{?sysusers_requires_compat}

%description server
The orchestrator server: receives NUT events through upssched, coordinates the desktop agent,
wakes a suspended desktop over Wake-on-LAN and powers UPSes off through upscmd.
%endif

%if %{with desktop}
%package desktop
Summary:        UPS Orchestrator desktop agent
Requires:       python3
Requires:       python3-requests
Requires:       python3-pydbus
Requires:       python3-gobject
# The shutdown/suspend prompt; without either the agent falls back to its default action
Recommends:     (zenity or kdialog)
Recommends:     libnotify
%{?systemd_requires}

%description desktop
The desktop side: a user agent that receives commands from the server and prompts the user,
and a system power agent that reports suspend/shutdown to the server over logind signals.
%endif

%prep
%autosetup

%build
# Pure Python, nothing to build.

%install
%if %{with server}
install -d %{buildroot}%{_datadir}/%{name}/server/app
install -pm644 server/app/*.py %{buildroot}%{_datadir}/%{name}/server/app/
%py_byte_compile %{python3} %{buildroot}%{_datadir}/%{name}/server/app
install -pm644 server/scripts/upssched.conf %{buildroot}%{_datadir}/%{name}/server/
install -Dpm755 server/scripts/upssched-cmd %{buildroot}%{_libexecdir}/%{name}/upssched-cmd
install -Dpm644 server/systemd/ups-orchestrator-server.service \
    %{buildroot}%{_unitdir}/ups-orchestrator-server.service
install -Dpm640 server/.env.example %{buildroot}%{_sysconfdir}/%{name}/server.env
install -Dpm640 server/ups_config.example.yml %{buildroot}%{_sysconfdir}/%{name}/ups_config.yml
install -Dpm440 packaging/rpm/ups-orchestrator-server.sudoers \
    %{buildroot}%{_sysconfdir}/sudoers.d/ups-orchestrator-server
install -Dpm644 %{SOURCE1} %{buildroot}%{_sysusersdir}/%{name}.conf
%endif

%if %{with desktop}
install -d %{buildroot}%{_datadir}/%{name}/desktop/app
install -pm644 desktop/app/*.py %{buildroot}%{_datadir}/%{name}/desktop/app/
%py_byte_compile %{python3} %{buildroot}%{_datadir}/%{name}/desktop/app
install -Dpm644 desktop/systemd/ups-power-agent.service \
    %{buildroot}%{_unitdir}/ups-power-agent.service
install -Dpm644 desktop/systemd/ups-orchestrator-agent.service \
    %{buildroot}%{_userunitdir}/ups-orchestrator-agent.service
install -Dpm644 desktop/.env.example %{buildroot}%{_sysconfdir}/%{name}/desktop.env
%endif

%if %{with server}
# The user and group exist before the files are laid down, so %attr(…,ups-orchestrator) sticks.
%pre server
%sysusers_create_compat %{SOURCE1}

%post server
%systemd_post ups-orchestrator-server.service

%preun server
%systemd_preun ups-orchestrator-server.service

%postun server
%systemd_postun_with_restart ups-orchestrator-server.service

%files server
%license LICENSE
%doc server/ups_config.example.yml
%dir %{_sysconfdir}/%{name}
%attr(0640,root,ups-orchestrator) %config(noreplace) %{_sysconfdir}/%{name}/server.env
%attr(0640,root,ups-orchestrator) %config(noreplace) %{_sysconfdir}/%{name}/ups_config.yml
%config(noreplace) %{_sysconfdir}/sudoers.d/ups-orchestrator-server
%dir %{_datadir}/%{name}
%{_datadir}/%{name}/server/
%{_libexecdir}/%{name}/
%{_unitdir}/ups-orchestrator-server.service
%{_sysusersdir}/%{name}.conf
%endif

%if %{with desktop}
%post desktop
%systemd_post ups-power-agent.service
%systemd_user_post ups-orchestrator-agent.service

%preun desktop
%systemd_preun ups-power-agent.service
%systemd_user_preun ups-orchestrator-agent.service

%postun desktop
%systemd_postun_with_restart ups-power-agent.service
%systemd_user_postun_with_restart ups-orchestrator-agent.service

%files desktop
%license LICENSE
%dir %{_sysconfdir}/%{name}
# Readable by the user agent, which runs as the logged-in user.
%config(noreplace) %{_sysconfdir}/%{name}/desktop.env
%dir %{_datadir}/%{name}
%{_datadir}/%{name}/desktop/
%{_unitdir}/ups-power-agent.service
%{_userunitdir}/ups-orchestrator-agent.service
%endif

%changelog
* Tue Oct 13 2026 Onur T. <onur@tatimur.com> - 0.1.0-1
- First packaged release: separate server (EL10) and desktop (Fedora) packages
