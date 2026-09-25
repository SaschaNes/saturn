# Backups for the Pacemaker/DRBD mode

Use an existing, independently managed backup product. Saturn does not take
backups or restore production data in this mode, and `backup_saturn.py` is
**only** for the legacy etcd mode. A replicated DRBD disk is not a backup:
deletions, ransomware, and corruption are replicated too.

Configure the backup product so that:

1. Its job runs only against the **currently promoted** node and the mounted
   Pacemaker-managed filesystem (`/srv/saturn/data` by default), never the
   backing devices or an unpromoted DRBD replica. Verify promotion and mount
   identity immediately before each job; fail closed during switchover.
2. It produces an application-consistent point-in-time copy. Coordinate any
   SMB application flush, filesystem freeze, and thaw with the backup product
   and its snapshot provider. Always thaw on error; do not leave the active
   filesystem frozen across a failover. A live file-by-file copy is not an
   atomic snapshot and can mix versions of files.
3. The destination is off-cluster, has restricted credentials and independent
   retention/immutability, and includes filesystem permissions, ACLs, extended
   attributes, ownership, and the matching Samba configuration and identity
   mapping. Protect encryption keys and backup credentials separately from
   the cluster and the repository.
4. Jobs that overlap a failover or report an incomplete snapshot are marked
   **failed**, not successful. Monitor job completion, age, usable capacity,
   and restore-test results externally.

Before production rollout, restore to an **isolated** test system and compare
representative SMB data and metadata. Document the restore point, recovery
time, and operator procedure with the chosen backup product. Do not restore
over the active DRBD resource or force a stale node Primary; a destructive
recovery needs its own reviewed procedure and explicit data-loss decision.
