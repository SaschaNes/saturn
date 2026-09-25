# Guided three-node installer

`ha/install.py` is a dependency-free, interactive **terminal wizard** for the
Pacemaker/DRBD profile. Run it as root on **one of the three cluster nodes**.
It checks mutual SSH access before changing anything, stages identical DRBD
and fencing configuration, creates the Corosync/Pacemaker cluster and BMC
resources, then requires independent fencing/storage verification before
submitting the Samba/VIP resources. Its terminal panels and confirmations work
over SSH; no web server, Node.js, or third-party Python package is needed.

**Not an unattended or in-place migration tool.** It never selects a DRBD
vendor package, formats a disk, calls `drbdadm create-md`, forces a Primary,
copies share data, sets up Samba accounts/shares, or claims production
acceptance. The three-node hardware and backup restore drill in
[ACCEPTANCE.md](ACCEPTANCE.md) remain mandatory. On failure the installer stops
without an automatic rollback; inspect the state before resuming.

## Before starting

1. Prepare three **new/dedicated** Ubuntu 24.04 or 26.04 nodes, independent
   backing disks, Corosync/SSH and DRBD networks, independent IPMI or Redfish
   BMCs, a VIP, matching hostnames, matching UID/GIDs, and identical Samba
   configuration on all nodes. Obtain a supported stable DRBD 9 kernel module
   and compatible DRBD utilities for the actual kernel on **each** node. Do not
   silently install the release-candidate PPA version in production. Install
   Ubuntu `python3`, `openssh-client`, `iproute2`, `util-linux`, `pacemaker`,
   `pcs`, `corosync`, `resource-agents-extra`, `samba`, and the selected
   `fence-agents-ipmilan` + `ipmitool` **or** `fence-agents-redfish`. Ensure the
   `ocf:linbit:drbd` agent is installed. Apply supported security updates.
2. **Exchange SSH keys first**, on every node. For example, create an ed25519
   key with `ssh-keygen -t ed25519` if that node has none, and use
   `ssh-copy-id root@<cluster-ip>` to authorize it on each of the *other two*
   nodes. Verify the server's host-key fingerprint using a trusted console or
   out-of-band inventory **before** accepting or pinning it in root's
   `known_hosts`. Do not use `StrictHostKeyChecking=no`, forward an agent, or
   copy a private key between nodes. Limit root SSH access to the management
   network and trusted cluster hosts; remove unnecessary cross-node access
   after provisioning if your operations model permits it. The installer
   checks all six directed connections using `BatchMode=yes` and strict host
   key verification. It runs commands locally on its own node, so self-SSH is
   not required. It never generates or exchanges SSH keys for you.
3. Stop and disable legacy `saturn.service`, independent `smbd.service`, and
   `drbd.service` startup on **all** nodes. Ensure the old VIP and share mount
   are absent. Back up data first; do not deploy over an existing Corosync
   cluster, DRBD resource file, or `/etc/saturn` installation. Load/verify the
   DRBD 9 module, confirm secure BMC credentials/certificates, and plan a
   maintenance window with out-of-band console access.

## Run the stages

Use a private path **outside Git**. Keep the same repository checkout on the
installation node for all stages. Do not copy real passwords into JSON or
shell history. The first stage is interactive and only writes a root-owned
`0600` JSON file (no cluster changes):

```sh
sudo python3 ha/install.py wizard --config /root/saturn-cluster.json
sudo python3 ha/render.py /root/saturn-cluster.json /root/saturn-plan
```

Review the plan, IPs, BMC System URI (for Redfish), device paths, service
interface, and backup strategy. `/root/saturn-plan` must not already exist.
Then run:

```sh
sudo python3 ha/install.py prepare --config /root/saturn-cluster.json
```

`prepare` confirms the hostname, Ubuntu version, installed commands/DRBD 9
module, node IPs, inactive independent services, empty cluster configuration,
and all six strict SSH paths **before any writes**. It prompts once for each
BMC password without echoing it and sends it over SSH stdin, not argv. It
installs the DRBD `.res`, private config, root-only secret readers/secrets, and
the Pacemaker marker on all nodes. It parses the DRBD configuration but
**does not activate DRBD or touch disk contents**. Partial failure is not
automatically retried: reconcile files manually before continuing.

`prepare` runs `/etc/saturn/preflight.py /etc/saturn/cluster.json` on **all**
nodes (the `bootstrap` stage repeats it). Set a strong `hacluster`
password on each node, start `pcsd`, and run `pcs host auth` **interactively**
on the installation node with all three names and `addr=<cluster-ip>`.
Do not use the `-p` option or put passwords into scripts/process arguments.
For example:

```sh
pcs host auth node1 addr=10.10.0.11 node2 addr=10.10.0.12 node3 addr=10.10.0.13 -u hacluster
sudo python3 ha/install.py bootstrap --config /root/saturn-cluster.json
```

`bootstrap` sets up and starts a fresh three-member cluster with the default
encrypted knet transport, verifies each node's preflight, and creates the
three BMC fence resources with `stonith-enabled=true` and
`no-quorum-policy=stop`. **Stop here.** In a maintenance window, test each
real BMC power-off with `pcs stonith fence <node>`, confirm the target is
actually off, then safely power it on and verify rejoining and quorum. The
installer cannot infer a working power-off path from agent metadata alone.
Fence the installation node last, or return to it after it rejoins.

Initialize and synchronize DRBD using a **separately reviewed storage
migration runbook**. `create-md`, `primary --force`, `mkfs.ext4`, and a data
copy can erase irreplaceable data; the installer intentionally does not issue
them. Verify all replicas `UpToDate` and connected, and ensure the initial
Primary has been demoted to Secondary. Verify ext4 was created on the **DRBD
device**, the target mount is free everywhere, Samba configuration and
authentication are tested, the BMC tests have succeeded, and your external
backup product has completed an isolated restore drill. Then:

```sh
sudo python3 ha/install.py resources --config /root/saturn-cluster.json
```

`resources` checks three online nodes, quorum, safe Pacemaker properties,
matching fence agents, an unmounted share, and all three DRBD replicas
Secondary/UpToDate with two established peer connections and quorum. It asks
for two explicit operator attestations, builds and verifies an offline CIB,
refuses concurrent CIB changes, and submits only the reviewed resource delta.
It does **not** automatically test a BMC or prove ext4 contents or application
data integrity. Run the complete [hardware acceptance](ACCEPTANCE.md) before
using the VIP for production clients. Do not bypass a failed check with
`--force`, disable STONITH, or force DRBD promotion.
