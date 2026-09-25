# Pacemaker / DRBD 9 profile (automatic failover)

This is a **different operating mode** from the legacy etcd-based `saturn.service`.
Pacemaker/Corosync owns DRBD promotion, the ext4 mount, Samba, and the VIP;
out-of-band IPMI or Redfish STONITH fences a failed primary. DRBD protocol C
and majority quorum protect writes that reach DRBD; application/page-cache
writes not explicitly synced are not automatically durable. **Never run this
profile alongside the legacy daemon or its `rsync` replication, and do not
let systemd start Samba independently.** This profile assumes three
*diskful* DRBD 9 nodes with the same backing-device path and independent BMCs.

The repository contains a **review-only configuration generator**, not an
installer. It does not execute `pcs`, initialize DRBD, format disks, or handle
BMC passwords. Offline CIB checks run against Ubuntu 24.04 and 26.04 tools,
but no three-node DRBD/BMC cluster was available for live failover testing.
Neither the template nor those checks certify a production installation.

Run the repository checks with `python3 -m unittest discover -s tests`. The
CI matrix runs these checks with the installed `pcs`, Pacemaker, DRBD utilities,
and fencing-agent metadata on Ubuntu 24.04 and 26.04. Offline CIB checks
deliberately bypass agent validation when no real DRBD device, Samba unit,
network interface, or BMC exists. The **production plan keeps strict
`--agent-validation` enabled** and requires successful hardware acceptance.

## Prerequisites

1. Ubuntu 24.04 or 26.04, Pacemaker, Corosync, `pcs`, `resource-agents-extra`,
   Samba, the `ocf:linbit:drbd` agent, DRBD 9 userspace and a DRBD 9 kernel
   module on every node. **For Ubuntu 26.04, the LINBIT PPA was incomplete as
   of September 2026; use the LINBIT customer repository or verify an
   equivalent supported package source.** Do not use an unverified in-kernel
   DRBD version in place of DRBD 9. Install `fence-agents-ipmilan` plus
   `ipmitool` for IPMI, or `fence-agents-redfish` for Redfish.
2. Three independent backing devices, a dedicated replication network,
   synchronized clocks, working cluster quorum, and independent management
   access to every BMC. Use unique credentials with only power-management
   privileges. Verify the chosen agent and `password_script` with
   `pcs stonith describe fence_ipmilan --full` or
   `pcs stonith describe fence_redfish --full`. Redfish uses verified TLS;
   install the BMC certificate authority in the system trust store on all
   nodes. Never disable TLS certificate verification to make fencing work.
3. A validated offline backup and recovery plan. DRBD replication is not a
   backup. The legacy `backup_saturn.py` relies on etcd and must **not** be
   used with this profile until it is migrated to Pacemaker state.
4. Identical Samba configuration, share path, UID/GID mapping, ACL/xattr
   support and authentication setup on all nodes. This is active/passive
   Samba, not CTDB: existing SMB sessions and open handles may disconnect
   during failover. Test client reconnection and application-level durability;
   do not advertise transparent SMB continuous availability based on the VIP.

## Generate the reviewed plan

Copy `ha/cluster.example.json` to a private config outside the repository.
Replace every TEST-NET address with real private addresses, and provide the
actual cluster hostnames, replication endpoints, BMC endpoints, backing device,
VIP, network interface and mount path. The example intentionally fails
validation so it cannot accidentally be deployed.
For Redfish set `fencing.agent` to `fence_redfish` and add a distinct
`systems_uri` (for example `/redfish/v1/Systems/System.Embedded.1`) to each
node. Confirm the URI with that BMC's Redfish API before generating a plan.

```sh
python3 ha/render.py /root/saturn-cluster.json /root/saturn-plan
```

The output contains a DRBD resource file, a **non-executable** `PLAN.md`, and
three secret-reader scripts. Review all output and install the DRBD file on
all nodes. Install the scripts to the configured `password_script_dir` on
**every** node, owned by root with mode 0700. Supply the actual passwords
separately as `/etc/saturn/fencing/<node>.password`, root-owned mode 0600.
Never put BMC secrets in the JSON config, git, the plan, or shell history.

Before creating any Pacemaker resources, stop and disable the old Saturn
service on **all** nodes, install `/etc/saturn/pacemaker-managed` as a
root-owned marker on all nodes, and stop/disable independent `smbd.service`
and `drbd.service` startup. The included legacy systemd unit refuses to start
when the marker exists; the old unit already installed in `/etc` or `/lib`
must also be removed/disabled. The marker alone does not stop an already
running process. Verify VIP and Samba are inactive everywhere.

Run `python3 ha/preflight.py /root/saturn-cluster.json` on **each** node before
applying the resource plan. It checks only local packages, DRBD major version,
the legacy/Samba service state, absence of the VIP, and fencing-secret file
permissions. It cannot test that fencing really powers off a node or that
DRBD data is synchronized.

Initialize and synchronize DRBD in a **separate, reviewed migration runbook**.
`create-md`, `primary --force`, `new-current-uuid`, `mkfs`, and `rsync --delete`
can destroy existing data; none is generated here. Before resource activation,
verify all three DRBD disks report `UpToDate`. Protect the filesystem with a
single DRBD Primary; never mount the underlying backing device directly.

Follow `PLAN.md` in a maintenance window. First establish Corosync membership
and a 3-node quorum, then configure **and test** all three BMC fencing
resources. Only after each fence test has demonstrably powered off its target
should you create the DRBD promotable resource, mount, Samba and VIP. Leave
`stonith-enabled=true` and `no-quorum-policy=stop`. DRBD majority quorum with
`on-no-quorum suspend-io` is a second, independent protection; it is not a
replacement for working STONITH.

## Acceptance and operations

Use the [hardware acceptance checklist](ACCEPTANCE.md) before any production
rollout. Offline tests validate configuration syntax, not end-to-end failover.

Test at least: planned primary moves in both directions; abrupt power loss;
cluster network partition; replication network partition; one failed BMC;
unsynchronized DRBD peer; full backing disk; reboot and return of an old
primary; and restore from an offline backup. Verify fencing occurs **before**
a new primary mounts ext4, and no two nodes ever host the VIP/mount. A write
workload with `fsync` from an SMB client must be checked for acknowledged data
across each scenario. On an unsafe or ambiguous state, **do not force a
promotion**: fence or repair the old primary, verify DRBD UpToDate and quorum,
and investigate Pacemaker failures first.

Operational commands include `pcs status`, `pcs constraint config --full`,
`pcs stonith config`, `drbdadm status saturn_data`, and `crm_mon --output-as xml`.
For a read-only check of the expected resource topology and Pacemaker quorum,
run `sudo python3 ha/status.py /root/saturn-cluster.json` from a cluster node.
This check cannot prove BMC power operations or that every DRBD replica is
UpToDate; those require independent validation.
For Patroni-like local observability, install `ha/status.py` to
`/usr/lib/saturn/ha/status.py` and the included `saturn-observer.service` on
each node, with the private configuration at `/etc/saturn/cluster.json`.
The observer only reads `crm_mon`; it **never** moves resources or assigns a
VIP. Its loopback-only `/status` endpoint always returns JSON (including
`healthy: false` on failure); `/health`, `/primary`, and `/standby` return 503
for unhealthy cluster state. `/standby` does not prove that DRBD data is
UpToDate. Do not expose these endpoints as a failover authority.
Use `pcs node standby <node>` for a planned move and `pcs node unstandby <node>`
after maintenance. Avoid `pcs resource move` rules left behind after tests.

References: [LINBIT DRBD 9 guide](https://linbit.com/drbd-user-guide/drbd-guide-9_0-en/),
[LINBIT 3-node Pacemaker guide (Ubuntu 24.04/26.04)](https://linbit.com/blog/highly-available-nfs-targets-with-drbd-pacemaker/),
and [ClusterLabs fencing guidance](https://clusterlabs.org/projects/pacemaker/doc/3.0/Pacemaker_Explained/html/fencing.html).
