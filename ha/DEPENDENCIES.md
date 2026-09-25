# Dependency and security-update review

Reviewed September 25, 2026, against the configured Ubuntu package indexes,
the LINBIT DRBD 9 PPA indexes, and the official GitHub Actions release. These
are **repository candidates**, not proof that any real cluster is patched or
free of vulnerabilities. Ubuntu commonly backports security fixes without
changing an upstream major version. Do not replace distribution packages
with an arbitrary upstream binary just to obtain a larger version number.

Saturn's Python programs use **only the standard library**: there is no pip,
npm, or Node.js application dependency to update. Node.js is used by the
GitHub-hosted `actions/checkout` step, not on Samba nodes. The workflow pins
the latest reviewed checkout release, **v7.0.1**, to its commit SHA; it uses
Node.js **24 LTS**, not end-of-life Node.js 20. Node.js 26 is a newer *Current*
release, not the action's supported runtime. Self-hosted Actions runners must
be at least version 2.327.1 for Node.js 24 actions. CI uses fresh Ubuntu
container indexes on every run; the containers do not install a DRBD kernel
module or perform real fencing.

## Ubuntu package candidates

| Package(s) | Ubuntu 24.04 | Ubuntu 26.04 | Used by |
| --- | --- | --- | --- |
| `python3` | 3.12.3-0ubuntu2.1 | 3.14.3-0ubuntu2 | Both modes |
| `pacemaker` | 2.1.6-5ubuntu2 | 3.0.1-1ubuntu2 | Automatic HA |
| `pcs` | 0.11.7-1ubuntu1 | 0.12.1-2ubuntu3 | Automatic HA |
| `corosync` | 3.1.7-1ubuntu3.2 | 3.1.9-2ubuntu3 | Automatic HA |
| `resource-agents-extra` | 1:4.13.0-1ubuntu4.2 | 1:4.17.0-1ubuntu2.1 | Automatic HA |
| `fence-agents-ipmilan`, `fence-agents-redfish` | 4.12.1-2~exp1ubuntu4 | 4.17.0-1ubuntu1 | Selected BMC agent |
| `ipmitool` | 1.8.19-7ubuntu0.24.04.3 | 1.8.19-10ubuntu1 | IPMI profile |
| `samba` | 4.19.5+dfsg-4ubuntu9.7 | 4.23.6+dfsg-1ubuntu2.2 | Both modes |
| Ubuntu `drbd-utils` | 9.22.0-1build1 | 9.22.0-1.2build1 | **Not the validated DRBD 9 stack** |
| Ubuntu `drbd-dkms` | Not packaged | Not packaged | Obtain a supported DRBD 9 module |
| `openssh-client`, `openssh-server` | 9.6p1-3ubuntu13.19 | 10.2p1-2ubuntu3.6 | Legacy SSH / host management |
| `openssl` | 3.0.13-0ubuntu3.15 | 3.5.5-1ubuntu3.5 | System TLS |
| `rsync` | 3.2.7-1ubuntu1.5 | 3.4.1+ds1-7ubuntu0.3 | Legacy/manual mode only |
| `iproute2` | 6.1.0-1ubuntu6.4 | 6.19.0-1ubuntu1.1 | Both modes |
| `etcd-server`, `etcd-client` | 3.4.30-1ubuntu0.24.04.3 | 3.5.16-10 | Legacy/manual mode only |

The LINBIT PPA currently lists `drbd-dkms` **9.3.4** for both releases and
`drbd-utils` **9.35.0-rc.1**. The latter is a *release candidate*. The lab
tested those packages only on Ubuntu 24.04 with kernel 6.8.0-139; their
availability on 26.04 does not establish module compatibility or vendor
support on that release. For production, obtain a supported stable source,
verify module/userspace compatibility and upgrade policy, and repeat the
[hardware acceptance tests](ACCEPTANCE.md) after upgrades. Do not mix the
Ubuntu DRBD utilities with the PPA DKMS module without explicit validation.
Fencing agents and some resource agents come from Ubuntu `universe`; confirm
who is responsible for timely security fixes on the chosen support plan.

## Repeat the audit on **each** production node

1. Record `cat /etc/os-release`, `uname -r`, `drbdadm --version`, the loaded
   DRBD module version, `pcs --version`, and `apt-cache policy` for every
   installed package above. Verify that the three nodes have compatible
   versions and that the kernel module survives a planned reboot.
2. Run `sudo apt-get update` and inspect `apt list --upgradable` and Ubuntu
   Security Notices. Install applicable security updates through the approved
   maintenance process, **one HA node at a time**, with tested fencing,
   quorum, backups, and failback procedures. Check vendor advisories for
   LINBIT, BMC firmware, and the existing backup product separately.
3. Retest DRBD synchronization, Pacemaker agent metadata, power-off fencing,
   SMB access through the VIP, and the external restore drill after relevant
   upgrades. Never use a blanket `dist-upgrade` across all nodes at once.

This inventory cannot declare "no security holes": it is a dated version
review, not a CVE scan of the installed package set, firmware, or hardware.
The legacy etcd/`rsync` mode remains **manual failover only** even if all its
packages are current.

References: [Ubuntu Security Notices](https://ubuntu.com/security/notices),
[Node.js release status](https://nodejs.org/en/about/previous-releases), and
[actions/checkout v7.0.1](https://github.com/actions/checkout/releases/tag/v7.0.1).
