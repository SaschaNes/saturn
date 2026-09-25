# Saturn

## Operating modes

**Automatic failover:** The [Pacemaker/DRBD 9 profile](ha/README.md) uses
three synchronous DRBD replicas, Corosync/Pacemaker, and tested IPMI or
Redfish fencing. `ha/render.py` creates a plan that requires operator review;
`ha/status.py` checks cluster state without changing resources. The plan must
be adapted to real hardware and pass failure tests before production use.
This repository cannot install or certify a production cluster by itself.
The [three-VM lab report](ha/LAB.md) records what was tested and what still
requires real hardware.
See the [dependency review](ha/DEPENDENCIES.md) for dated Ubuntu package
candidates and security-update procedures.

**Legacy/manual mode:** The remainder of this page describes the existing
etcd/`rsync` coordinator. It must **never run alongside** the Pacemaker
profile and still has no automatic failover. `backup_saturn.py` supports
only this legacy mode.

Saturn controls a **single Samba VIP** on a three-node cluster using an etcd v3 lease and an atomic compare-and-swap transaction. It copies files from the active node to the standby nodes with `rsync`.

> **This is not Patroni and does not provide automatic failover.** Asynchronous `rsync` cannot guarantee that a standby has every acknowledged write; without fencing, an isolated old leader might keep serving clients. Every promotion requires a one-use operator approval after the previous leader has been isolated and the intended node's data verified. Do not use this for unattended HA or as a substitute for synchronous storage and fencing.

## Requirements

- Three Linux hosts running supported Ubuntu 24.04 or 26.04 packages (including
  their security updates), the distribution's supported Python 3 (3.12 on
  24.04; 3.14 on 26.04), etcd v3 with the gRPC-JSON gateway enabled, Samba,
  `rsync`, OpenSSH and `iproute2`. Do not deploy with end-of-life Python 3.9.
- A three-member etcd cluster with quorum, preferably authenticated TLS endpoints. All nodes need gateway access to etcd; etcd must not be exposed to untrusted networks.
- Passwordless **root SSH** from the primary to the standby nodes for replication and from the backup host to cluster nodes. Pin SSH host keys in `known_hosts`. Limit these credentials to trusted machines.
- The same Samba configuration, path, UID/GID mapping, ACL and xattr support on all nodes. Only the active node may accept writes. The share directory must actually contain the intended data before approving a takeover.
- A separate, durable backup volume (not `/tmp`, and ideally off-cluster).

## Installation

1. Configure an etcd v3 cluster separately, enable its v3 JSON gateway and verify that `/v3/kv/range` is reachable with the configured TLS credentials. The old etcd v2 member-leader configuration is **not** used.
2. Stop/disable the old Saturn service on every node before replacing it; preserve a verified backup and reconcile the existing share data. Install `saturn.py` and `backup_saturn.py` into `/usr/lib/saturn/`, and `lib/systemd/system/saturn.service` into `/etc/systemd/system/saturn.service`. Copy `etc/saturn/config.example.json` to `/etc/saturn/config.json` on each node; set its local `node`, all node addresses, endpoints, TLS files, VIP, interface, and absolute share path. The share path must be a mount point by default (`require_mount: true`); set it to `false` only if the share deliberately lives on the root filesystem. Protect config and TLS keys with root permissions. Install/test SSH keys and Samba separately.
3. Disable Samba's own automatic startup so only Saturn starts it, then run `systemctl daemon-reload && systemctl enable --now saturn.service` on each node. The service starts in **standby** and removes a stale local VIP.
4. From the chosen node, after verifying that no other node serves the VIP and its data is current, run:

   ```sh
   python3 /usr/lib/saturn/saturn.py status
   python3 /usr/lib/saturn/saturn.py approve --old-leader-fenced --data-verified
   ```

   The two flags are operator attestations, **not automatic checks**. On initial bootstrap, verify all nodes are inactive before using them. The running daemon consumes the approval exactly once and acquires a short-lived leased key before adding the VIP and starting Samba. The approved node must have its service running.

## Failover and recovery

If etcd is unavailable, the lease cannot be refreshed, promotion fails, or the service shuts down, Saturn stops replication and Samba and removes its VIP. It never promotes another node automatically. A failed demotion requires **manual isolation** of that host; an etcd lease alone cannot remove an IP from a machine that is hung or partitioned.

For a takeover: isolate or power off the previous primary and verify its VIP/Samba are down; confirm that etcd no longer has an `owner`; inspect replication status and select/repair the most up-to-date standby. Then check `status` and approve *on that node*. **Never approve a standby just because the lease expired.** Old clients and a returning former primary must not write to the share. To restore the old primary, reconcile its files from the current primary before letting it join as standby. The new primary's first `rsync --delete` will otherwise overwrite stale/unique files on that node.

On every leader renewal, the coordinator checks ownership via an etcd transaction; changes in ownership or connectivity trigger demotion. `last_owner` remains in etcd for operator visibility. All other nodes remain in standby until explicitly approved. A stale, unused approval can be removed with `python3 /usr/lib/saturn/saturn.py cancel-approval`. Use `journalctl -u saturn.service` to inspect coordination and replication failures. Each lease is 60 seconds by default; successful replication is **not** a guarantee that all Samba writes are durable on all nodes.

### Planned switchover

For **planned** maintenance, make sure clients have stopped writing, the target's Saturn service is running, its share filesystem is mounted, and Samba is inactive there. On the current primary:

```sh
python3 /usr/lib/saturn/saturn.py switchover --target node2
```

The primary observes the request, stops Samba and removes its VIP, then runs a **final `rsync`** to the target while continuing to renew its etcd lease. Only after the copy succeeds does it atomically release ownership and issue a one-use approval for the target. The target acquires its own lease before starting Samba. A failed final copy does **not** approve the target; investigate and recover manually. This is not automatic failover from a crashed or partitioned primary. Never use it while other processes can write to the share outside Samba. A stale, unconsumed request can be removed with `python3 /usr/lib/saturn/saturn.py cancel-switchover` after inspecting cluster state.

### Local status API

Each daemon serves a read-only status API on `127.0.0.1:8008` (configurable port). `GET /status` returns the local role, recent etcd connectivity, and per-target last successful replication times. `GET /health`, `/primary`, and `/standby` return HTTP 200 only when the corresponding local state is recently validated; otherwise they return 503. For example, `curl -fsS http://127.0.0.1:8008/primary`. **`/standby` does not certify replication freshness or eligibility for failover.** The API binds only to loopback and does not perform administrative actions.

## Backups

Run on a separate backup host with `saturn.py`, `backup_saturn.py`, Python, rsync, SSH keys, etcd access and a local `config.json`:

```sh
python3 /usr/lib/saturn/backup_saturn.py backup daily --retain 7
python3 /usr/lib/saturn/backup_saturn.py backup weekly --retain 4
python3 /usr/lib/saturn/backup_saturn.py backup monthly --retain 12
```

Use a scheduler for these commands. Each run writes a new timestamped directory under `/var/backups/saturn/<period>/`, publishes it only after `rsync` succeeds and the owner remains unchanged, then retains the specified number of complete snapshots. The backup is **not application-consistent** if clients modify files during copying; arrange quiescence or a filesystem snapshot if that is required.

Restore requires a maintenance window: stop Saturn/Samba on every node, disconnect clients, ensure etcd has neither `owner` nor a pending `approval`, and restore to a chosen node:

```sh
python3 /usr/lib/saturn/backup_saturn.py restore daily 20260925T120000Z --target node1 --services-stopped
```

Verify the restored data before restarting the services and approving that node. Restore uses `--delete` on the target share; **it overwrites that node's data**. Never run it against a live share.

## Limits

This service is a fail-closed coordinator with **manual** promotion, not full high availability. For safe *automatic* failover, add a tested fencing mechanism and consistent/synchronous storage or an application-level replication protocol with a known safe promotion point. Validate partitions, lost quorum, slow `rsync`, interrupted restore, and crash/restart behavior in a non-production cluster before use.
