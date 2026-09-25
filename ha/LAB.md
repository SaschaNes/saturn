# Three-VM integration lab (Ubuntu 24.04)

This is a **test result**, not approval for a production deployment. The lab
used three isolated KVM guests in WSL on September 25, 2026. Its VM images,
temporary cluster configuration, SSH keys, and passwords live outside Git;
none is required or suitable for deployment to real hardware.

## Environment and scope

| Component | Tested version/configuration |
| --- | --- |
| Guest OS/kernel | Ubuntu 24.04.5, `6.8.0-139-generic` |
| DRBD | LINBIT Noble PPA `drbd-dkms` 9.3.4, `drbd-utils` 9.35.0-rc.1 |
| Pacemaker / pcs | 2.1.6 / 0.11.7 (Ubuntu packages) |
| Storage | Three independent **blank** 2 GiB virtual disks, DRBD protocol C |
| Network | Corosync `10.213.0.0/24`, DRBD `10.213.2.0/24`, VIP `10.213.0.100` |
| Fencing | Three `fence_virsh` resources controlling libvirt domains from outside the guests |

Both lab subnets used the **same virtual NIC/bridge**, so a physical network
partition or independent management network was **not** validated. The
lab-only `fence_virsh` configuration is not a replacement for the production
`fence_ipmilan` or `fence_redfish` profile; no BMC or TLS interoperability was
tested. The PPA's release-candidate DRBD userspace was suitable for this
experiment, **not** automatically a supported production package choice.
Ubuntu 26.04 has offline Pacemaker/agent tests in CI but **no** DRBD 9 kernel
integration test yet.

## What was actually checked

1. `ha/render.py` produced the DRBD resource and Pacemaker plan from a private
   three-node lab configuration. DRBD 9 parsed that resource on all three
   guests; after initial synchronization each disk reported `UpToDate`.
2. A real three-member Corosync cluster acquired quorum. Each libvirt fence
   agent was exercised through `pcs stonith fence <node>`: the corresponding
   **VM was confirmed shut off** before it was restarted and rejoined.
3. The generated resource commands passed `--agent-validation` with installed
   agents, `crm_verify` passed, and the reviewed CIB was applied. Pacemaker
   promoted one DRBD replica, mounted ext4, started Samba, then assigned the
   VIP. The other two disks remained unpromoted and `UpToDate`.
4. A guest-mode SMB client wrote a test file through the VIP. After
   `pcs node standby node1` moved the group to node2, a read through the VIP
   returned the original SHA-256 digest. This was **not** a continuous SMB
   workload or a client-side `fsync` durability test.
5. With all replicas synchronized, abruptly powering off the active node2 VM
   triggered fencing and automatic recovery on node1. The Pacemaker log showed
   successful node2 fencing at 07:52:47 UTC, DRBD promotion at 07:52:47,
   filesystem start at 07:52:48, and VIP start at 07:52:50. SMB data matched
   its checksum after recovery.
6. To test a fencing outage, the lab's **separate** SSH fencing endpoint was
   stopped and the active node1 VM powered off. Although node2 and node3 had
   quorum, the old owner remained `UNCLEAN` and **neither** mounted ext4 nor
   acquired the VIP. When the fencing endpoint returned, Pacemaker confirmed
   the VM was off, promoted node2, and restored the group; the SMB file's
   digest still matched.
7. With node1 already off, abruptly powering off primary node2 left node3
   without quorum. It did not claim the VIP. Restarting the last valid primary
   restored quorum and service; node1 subsequently rejoined as an
   `UpToDate` unpromoted replica.

The guests did not finish an ACPI/systemd power-off under this nested-KVM
environment. At the end of the test, the Samba/DRBD resources were disabled
and stopped, Pacemaker/Corosync were stopped on all three nodes, and guest
shutdown was requested. The remaining QEMU processes were then stopped via
libvirt. This lab result must **not** be read as validation of a clean guest
shutdown path.

The three libvirt domains remain defined but **shut off**. Their images are
under `/var/lib/libvirt/images/saturn-lab/`; the private lab configuration and
keys are under `/home/neste/saturn-lab/`, outside the repository. The
lab-only SSH fencing listener has been stopped. To reuse these VMs, restore
the isolated bridge and fencing listener, start the domains, and intentionally
re-enable the disabled Pacemaker resources; do not assume they are ready to
serve data after a WSL restart.

## Remaining production gates

- Run the [hardware acceptance checklist](ACCEPTANCE.md) on three **real**
  nodes, including separate replication and BMC networks, actual IPMI or
  Redfish power-off, long partitions, failed disks, Samba client durability,
  recovery, and the [external backup restore drill](BACKUP.md).
- Validate a supported, non-release-candidate DRBD 9 module/userspace source
  on the exact Ubuntu/kernel combination, including Ubuntu 26.04.
- Never copy the lab's guest access, Samba guest share, `fence_virsh` settings,
  subnet addresses, or fencing keys into the production plan.
