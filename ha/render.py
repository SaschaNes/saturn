#!/usr/bin/env python3
"""Render a review-only Pacemaker/DRBD deployment plan; never alter a cluster."""

import argparse
import ipaddress
import json
import re
from pathlib import Path


IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")
ABSOLUTE = re.compile(r"/[A-Za-z0-9_./-]+\Z")
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(network) for network in
                         ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def valid_path(path, prefix):
    if not isinstance(path, str) or not ABSOLUTE.fullmatch(path) or ".." in Path(path).parts or path == "/":
        raise ValueError(prefix + " must be a safe absolute path")
    return path


def valid_ip(value):
    ip = ipaddress.ip_address(value)
    if not isinstance(ip, ipaddress.IPv4Address) or not any(ip in net for net in PRIVATE_NETWORKS):
        raise ValueError("Use real private IPv4 addresses, not documentation or public addresses")
    return ip


def validate(config):
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON object")
    if not isinstance(config.get("cluster_name", "saturn"), str) or not IDENTIFIER.fullmatch(
            config.get("cluster_name", "saturn")):
        raise ValueError("Invalid cluster name")
    nodes = config["nodes"]
    if not isinstance(nodes, list) or len(nodes) != 3:
        raise ValueError("Exactly three diskful nodes are required")
    names, addresses = set(), set()
    for node in nodes:
        name = node["name"]
        if not isinstance(name, str) or not IDENTIFIER.fullmatch(name) or name in names:
            raise ValueError("Unique, safe node names are required")
        names.add(name)
        if not IDENTIFIER.fullmatch(node["bmc_user"]):
            raise ValueError("Invalid BMC username")
        for field in ("cluster_ip", "replication_ip", "bmc_ip"):
            address = valid_ip(node[field])
            if address in addresses:
                raise ValueError("All cluster, replication and BMC addresses must be distinct")
            addresses.add(address)
    drbd = config["drbd"]
    if not IDENTIFIER.fullmatch(drbd["resource"]):
        raise ValueError("Invalid DRBD resource name")
    for field in ("device", "backing_device"):
        valid_path(drbd[field], field)
    if drbd["device"] == drbd["backing_device"] or not 1024 <= drbd["port"] <= 65535:
        raise ValueError("Invalid DRBD device/backing device or port")
    fs = config["filesystem"]
    valid_path(fs["mount"], "mount")
    if fs["fstype"] != "ext4":
        raise ValueError("This single-primary plan supports ext4 only")
    vip = ipaddress.ip_interface(config["vip"]["address"])
    if not isinstance(vip, ipaddress.IPv4Interface) or vip.ip in addresses:
        raise ValueError("The VIP must be a distinct IPv4 address")
    valid_ip(str(vip.ip))
    if not IDENTIFIER.fullmatch(config["vip"]["interface"]):
        raise ValueError("Invalid VIP interface name")
    fence = config["fencing"]
    if fence["agent"] not in ("fence_ipmilan", "fence_redfish"):
        raise ValueError("Only fence_ipmilan and fence_redfish are supported")
    valid_path(fence["password_script_dir"], "password_script_dir")
    if fence["agent"] == "fence_redfish":
        for node in nodes:
            uri = node.get("systems_uri")
            if not isinstance(uri, str) or not re.fullmatch(r"/redfish/v1/Systems/[A-Za-z0-9_.-]+", uri):
                raise ValueError("Each Redfish node needs a specific /redfish/v1/Systems/... URI")
    return config


def render_drbd(config):
    drbd, nodes = config["drbd"], config["nodes"]
    lines = [f'resource "{drbd["resource"]}" {{',
             "  options {", "    auto-promote no;", "    quorum majority;",
             "    on-no-quorum suspend-io;", "    on-no-data-accessible suspend-io;", "  }",
             "  net { protocol C; }", "  volume 0 {",
             f'    device "{drbd["device"]}";', f'    disk "{drbd["backing_device"]}";',
             "    meta-disk internal;", "  }"]
    for index, node in enumerate(nodes):
        lines += [f'  on "{node["name"]}" {{',
                  f'    address {node["replication_ip"]}:{drbd["port"]};',
                  f"    node-id {index};", "  }"]
    hostnames = " ".join('"' + node["name"] + '"' for node in nodes)
    lines += [f"  connection-mesh {{ hosts {hostnames}; }}", "}"]
    return "\n".join(lines) + "\n"


def render_plan(config):
    nodes, drbd, vip, fs = (config[key] for key in ("nodes", "drbd", "vip", "filesystem"))
    resource = drbd["resource"]
    lines = ["# Review-only deployment plan (NOT an executable script)", "",
             "Do not proceed until the three BMCs, their independent network, DRBD 9 kernel",
             "module, Pacemaker agents, backups, and a restore drill have been verified.",
             "Never run the legacy Saturn service, etcd coordinator, or rsync replication",
             "on this cluster. Stop/disable/mask saturn.service on ALL nodes first.",
             "Passwords are NOT stored here: install each root-owned password script and",
             "its separate 0600 secret file on every node via a secure channel.", "",
             "## 1. Establish a three-node Pacemaker/Corosync cluster separately", "",
             "Verify `pcs status` shows three online nodes and quorum. Install the DRBD",
             "configuration on all nodes. Bring up and initialize DRBD under a change",
             "window; DO NOT format or overwrite existing data based on this plan.",
             "Verify all three disks are UpToDate before enabling Samba resources.", "",
             "## 2. Configure fencing FIRST (run on one cluster node)", "", "```sh",
              "pcs property set stonith-enabled=true no-quorum-policy=stop stonith-timeout=120s"]
    for node in nodes:
        name = node["name"]
        secret = config["fencing"]["password_script_dir"] + "/" + name
        agent = config["fencing"]["agent"]
        agent_options = ("lanplus=1 method=onoff" if agent == "fence_ipmilan"
                         else f'systems_uri={node["systems_uri"]} ssl_secure=1')
        lines += [f'pcs stonith create fence_{name} {agent} ip={node["bmc_ip"]} '
                  f'username={node["bmc_user"]} password_script={secret} {agent_options} '
                  f'pcmk_host_list={name} op monitor interval=60s --agent-validation',
                  f"pcs constraint location fence_{name} avoids {name}"]
    lines += ["```", "", "Stop here and test all three fencing paths in an isolated maintenance",
              "window. Confirm the affected server is really OFF before its resources can",
              "start elsewhere. Never set stonith-enabled=false or no-quorum-policy=ignore.",
              "", "## 3. Create the resources in a fresh CIB copy", "", "```sh",
              "pcs cluster cib saturn.cib",
              f"pcs -f saturn.cib resource create p_drbd_saturn ocf:linbit:drbd drbd_resource={resource} "
              "op start interval=0s timeout=40s stop interval=0s timeout=100s "
              "monitor interval=31s timeout=20s role=Unpromoted "
              "monitor interval=29s timeout=20s role=Promoted --agent-validation",
              "pcs -f saturn.cib resource promotable p_drbd_saturn "
              "meta promoted-max=1 promoted-node-max=1 clone-max=3 clone-node-max=1 notify=true",
              f'pcs -f saturn.cib resource create p_fs_saturn ocf:heartbeat:Filesystem '
              f'device={drbd["device"]} directory={fs["mount"]} fstype=ext4 run_fsck=no '
              'op start interval=0s timeout=60s stop interval=0s timeout=60s '
              'monitor OCF_CHECK_LEVEL=0 interval=15s timeout=40s --agent-validation',
              "pcs -f saturn.cib resource create p_smb_saturn systemd:smbd "
              "op start interval=0s timeout=60s stop interval=0s timeout=60s "
              "monitor interval=20s timeout=30s --agent-validation",
              f'pcs -f saturn.cib resource create p_vip_saturn ocf:heartbeat:IPaddr2 '
              f'ip={vip["address"].split("/")[0]} cidr_netmask={vip["address"].split("/")[1]} '
              f'nic={vip["interface"]} op monitor interval=20s timeout=20s --agent-validation',
              "pcs -f saturn.cib resource group add g_saturn p_fs_saturn p_smb_saturn p_vip_saturn",
              "pcs -f saturn.cib constraint order promote p_drbd_saturn-clone then start g_saturn",
              "pcs -f saturn.cib constraint colocation add g_saturn with p_drbd_saturn-clone "
              "score=INFINITY with-rsc-role=Promoted",
              "pcs -f saturn.cib constraint config --full",
              "crm_verify -x saturn.cib -V",
              "# ONLY after review and fencing/DRBD tests:",
              "pcs cluster cib-push saturn.cib", "```", "",
              "## 4. Acceptance", "",
              "Verify ordering, colocation, quorum and stonith in the live CIB; check",
              "DRBD UpToDate status on every node. Test orderly moves, hard power loss,",
              "network partitions and recovery with a continuous SMB write workload.",
              "Do not declare the cluster production-ready without real failover testing."]
    return "\n".join(lines) + "\n"


def render_secrets(config):
    return {node["name"]: "#!/bin/sh\nexec cat /etc/saturn/fencing/" + node["name"] + ".password\n"
            for node in config["nodes"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        config = validate(json.loads(args.config.read_text()))
        args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    (args.output / (config["drbd"]["resource"] + ".res")).write_text(render_drbd(config))
    (args.output / "PLAN.md").write_text(render_plan(config))
    scripts = args.output / "fence-scripts"
    scripts.mkdir(mode=0o700)
    for name, contents in render_secrets(config).items():
        script = scripts / name
        script.write_text(contents)
        script.chmod(0o700)
    print("Review-only artifacts created in", args.output)


if __name__ == "__main__":
    main()
