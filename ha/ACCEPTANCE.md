# HA acceptance and disaster-recovery checklist

Do not enable this profile for production until the following tests pass on
the **actual three-node hardware** with the production DRBD, Samba, network,
and BMC versions. WSL and offline CIB tests are not substitutes.

## Before testing

- [ ] A restorable backup exists outside the cluster. Complete a restore drill
      to a separate test system. Replication is not a backup.
- [ ] All three Pacemaker members have quorum, all three DRBD copies report
      `UpToDate`, and the cluster is not in maintenance mode.
- [ ] `stonith-enabled=true`, `no-quorum-policy=stop`, DRBD majority quorum,
      `on-no-quorum suspend-io`, and DRBD `auto-promote no` are confirmed in
      the **live** configuration, not only in rendered files.
- [ ] The legacy Saturn daemon and independent Samba/DRBD systemd startup are
      disabled. Exactly one node owns the ext4 mount, Samba, and the VIP.
- [ ] Each BMC is reachable via an independent management path. For Redfish,
      the certificate chain is trusted and `ssl_secure=1`; for IPMI, verify
      `method=onoff` actually waits until the host is powered off.

## Required failure scenarios

Perform these in a maintenance window with console/BMC access. **Fencing
commands power off physical servers.** Record timestamps, logs, SMB client
results, data hashes, and the node that owns each resource after every test.

1. **Planned moves:** Move the primary to each of the other two nodes using
   `pcs node standby <current-primary>`. Check DRBD promotion occurs before
   the filesystem mount and Samba/VIP start. Undo with `pcs node unstandby`.
2. **Real primary power loss:** Power off the active host through its BMC, not
   via a clean `systemctl stop`. Confirm Pacemaker observes verified fencing
   before promoting another DRBD copy. A second VIP or ext4 mount is a hard
   failure. Rejoin the old primary only after resynchronization.
3. **Cluster-network partition:** Isolate one node from Corosync while the
   BMC network remains available. The minority must not keep serving writes;
   the majority must not expose the VIP until the old owner is fenced.
4. **Replication-network partition:** Break DRBD connectivity separately from
   Corosync. Confirm DRBD quorum and disk-state checks prevent promotion of an
   outdated node. Do not force an out-of-date disk to Primary.
5. **BMC failure:** Make one fencing endpoint unavailable in a test window.
   If the current owner cannot be proven stopped, replacement resources must
   not start. Restore the BMC before recovering the cluster.
6. **Storage faults:** Test a failed backing device, a full filesystem, and a
   reboot while resynchronization is incomplete. No service may start on a
   DRBD copy that is not eligible for safe promotion.
7. **Client behavior:** Run a continuous SMB workload that explicitly flushes
   data and records acknowledged operations. Verify those operations and
   checksums after each failover. Document the reconnection delay and any
   dropped sessions. This is active/passive Samba, **not** transparent SMB
   continuous availability or a promise that buffered writes are durable.
8. **Recovery:** Restore a backup in an isolated recovery cluster and verify
   share permissions, ACLs, extended attributes, ownership, and application
   data. Practice a return to service without `primary --force` on live data.

Review `pcs status`, `pcs stonith config`, `pcs constraint config --full`,
`drbdadm status <resource>`, and `journalctl` after each scenario. Capture a
`crm_report` and preserve BMC event logs for failures. Investigate any
unexpected fencing, lost quorum, I/O suspension, or stale data before
production rollout. Never bypass STONITH or set `no-quorum-policy=ignore` to
make a failed test pass.
