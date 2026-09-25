#!/usr/bin/env python3
"""Interactive, staged three-node HA installer. Run as root on a cluster node."""

import argparse
import getpass
import hashlib
import json
import os
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

if __package__:
    from . import render
else:
    import render


CLUSTER_CONFIG = "/etc/saturn/cluster.json"
MARKER = "/etc/saturn/pacemaker-managed"
PACKAGES = ("pcs", "crm_mon", "drbdadm", "drbdsetup", "smbd", "fence_ipmilan",
            "fence_redfish", "crm_verify", "ipmitool", "findmnt")
SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ConnectTimeout=8", "-o", "ForwardAgent=no",
               "-o", "ClearAllForwardings=yes")


class InstallationError(Exception):
    """Stop at the current stage without guessing how to recover."""


def run(args, *, input_data=None, timeout=45, check=True):
    result = subprocess.run(args, input=input_data, text=True, capture_output=True,
                            timeout=timeout)
    if check and result.returncode:
        raise InstallationError(f"{shlex.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


def remote(node, args, *, input_data=None, timeout=45, check=True):
    # Never use a shell for local arguments. SSH has a remote shell: quote each
    # validated argument, and NEVER put passwords in the command or argv.
    if node["name"] == socket.gethostname().split(".")[0]:
        return run(args, input_data=input_data, timeout=timeout, check=check)
    cmd = ["ssh", *SSH_OPTIONS, "root@" + node["cluster_ip"], shlex.join(args)]
    return run(cmd, input_data=input_data, timeout=timeout, check=check)


def banner(title, step=None):
    color = "\033[36m" if sys.stdout.isatty() and not os.environ.get("NO_COLOR") else ""
    end = "\033[0m" if color else ""
    print(f"\n{color}╭{'─' * 62}╮\n│  SATURN HA  {title[:48]:<49}│\n╰{'─' * 62}╯{end}")
    if step:
        print(f"  {step}\n")


def ask(label, default=None):
    suffix = f" [{default}]" if default is not None else ""
    while True:
        answer = input(f"  {label}{suffix}: ").strip()
        if answer or default is not None:
            return answer or default
        print("  Required field; please enter a value.")


def wizard():
    banner("Configuration wizard", "1/3 · Three diskful nodes; enter real private addresses.")
    cluster_name = ask("Cluster name", "saturn")
    nodes = []
    for index in range(1, 4):
        print(f"\n  Node {index}")
        node = {key: ask(label, default) for key, label, default in (
            ("name", "Hostname", f"node{index}"),
            ("cluster_ip", "Cluster/SSH IPv4", None),
            ("replication_ip", "DRBD replication IPv4", None),
            ("bmc_ip", "Independent BMC IPv4", None),
            ("bmc_user", "BMC user (not a password)", "fencer"))}
        nodes.append(node)
    banner("Storage and service", "2/3 · No disk will be erased by this wizard.")
    drbd = {"resource": ask("DRBD resource", "saturn_data"),
            "device": ask("DRBD device", "/dev/drbd1000"),
            "backing_device": ask("Dedicated backing device (same path on each node)"),
            "port": int(ask("DRBD TCP port", "7788"))}
    fs = {"mount": ask("SMB filesystem mount", "/srv/saturn/data"), "fstype": "ext4"}
    vip = {"address": ask("Service VIP/CIDR"), "interface": ask("VIP interface")}
    banner("Power fencing", "3/3 · Passwords are entered only during deployment.")
    choice = ask("Fencing agent (1 = IPMI, 2 = Redfish)", "1")
    if choice not in ("1", "2"):
        raise InstallationError("Choose 1 or 2 for fencing")
    agent = "fence_ipmilan" if choice == "1" else "fence_redfish"
    if agent == "fence_redfish":
        for node in nodes:
            node["systems_uri"] = ask(f"{node['name']} Redfish systems URI")
    config = {"cluster_name": cluster_name, "nodes": nodes, "drbd": drbd, "filesystem": fs, "vip": vip,
              "fencing": {"agent": agent, "password_script_dir": "/usr/local/libexec/saturn/fence"}}
    return render.validate(config)


def review(config):
    banner("Review configuration", "No BMC passwords are shown or stored in JSON.")
    print(f"  {'Node':<16} {'Cluster / SSH':<17} {'Replication':<17} BMC")
    for node in config["nodes"]:
        print(f"  {node['name']:<16} {node['cluster_ip']:<17} "
              f"{node['replication_ip']:<17} {node['bmc_ip']}")
    print(f"\n  DRBD: {config['drbd']['resource']} · {config['drbd']['backing_device']}")
    print(f"  Cluster: {config.get('cluster_name', 'saturn')}")
    print(f"  Mount: {config['filesystem']['mount']} · VIP: {config['vip']['address']}")
    print(f"  Fencing: {config['fencing']['agent']}")


def save_config(path, config):
    path = Path(path)
    if path.is_symlink():
        raise InstallationError("Refusing a symlink for the private configuration")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(config, stream, indent=2)
        stream.write("\n")


def load_config(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or (info.st_mode & 0o077) or info.st_uid != 0:
        raise InstallationError("Private configuration must be a root-owned regular file, inaccessible to group/other")
    return render.validate(json.loads(path.read_text()))


def confirm(phrase):
    if input(f"\n  Type {phrase!r} to continue: ").strip() != phrase:
        raise InstallationError("Not confirmed; no further changes made")


def local_node(config):
    name = run(["hostname", "-s"]).stdout.strip()
    if name not in [node["name"] for node in config["nodes"]]:
        raise InstallationError(f"This host ({name}) is not one of the configured nodes")
    return name


# Executed over SSH; only outputs non-secret system facts. The installer does
# not install unreviewed third-party DRBD packages or probe BMC credentials.
PROBE = """import json,os,pathlib,shutil,socket,subprocess
osrel={}
for line in pathlib.Path('/etc/os-release').read_text().splitlines():
 if '=' in line:
  k,v=line.split('=',1);osrel[k]=v.strip('"')
def service(name):
 return subprocess.run(['systemctl','is-active','--quiet',name]).returncode == 0
addrs=json.loads(subprocess.check_output(['ip','-j','address']))
ips=[item['local'] for dev in addrs for item in dev.get('addr_info',[]) if item['family']=='inet']
print(json.dumps({'name':socket.gethostname().split('.')[0], 'os':osrel.get('ID'),
 'version':osrel.get('VERSION_ID'),'ips':ips,
 'commands':{name:bool(shutil.which(name)) for name in %r},
 'ocf':{name:pathlib.Path('/usr/lib/ocf/resource.d/'+name).is_file() for name in
        ('linbit/drbd','heartbeat/Filesystem','heartbeat/IPaddr2')},
 'drbd9':pathlib.Path('/proc/drbd').read_text().startswith('version: 9.') if pathlib.Path('/proc/drbd').exists() else False,
 'legacy_active':service('saturn.service'),'samba_active':service('smbd.service'),
 'legacy_enabled':subprocess.run(['systemctl','is-enabled','--quiet','saturn.service']).returncode==0,
 'samba_enabled':subprocess.run(['systemctl','is-enabled','--quiet','smbd.service']).returncode==0,
 'drbd_enabled':subprocess.run(['systemctl','is-enabled','--quiet','drbd.service']).returncode==0,
 'cluster_exists':pathlib.Path('/etc/corosync/corosync.conf').exists(),
 'marker_exists':pathlib.Path(%r).exists(),
 'config_exists':pathlib.Path(%r).exists()}))
""" % (PACKAGES, MARKER, CLUSTER_CONFIG)


def inspect_nodes(config, *, fresh):
    local_node(config)
    problems = []
    for node in config["nodes"]:
        name = node["name"]
        try:
            facts = json.loads(remote(node, ["python3", "-c", PROBE]).stdout)
        except (InstallationError, ValueError, subprocess.TimeoutExpired) as exc:
            problems.append(f"{name}: SSH or Python probe failed: {exc}")
            continue
        if (facts["name"], facts["os"]) != (name, "ubuntu"):
            problems.append(f"{name}: hostname/OS mismatch: {facts['name']}, {facts['os']}")
        if facts["version"] not in ("24.04", "26.04"):
            problems.append(f"{name}: unsupported Ubuntu {facts['version']}")
        for key in ("cluster_ip", "replication_ip"):
            if node[key] not in facts["ips"]:
                problems.append(f"{name}: {key} is not assigned to this host")
        vip = config["vip"]["address"].split("/")[0]
        if vip in facts["ips"]:
            problems.append(f"{name}: VIP is already assigned outside Pacemaker")
        if remote(node, ["ip", "link", "show", "dev", config["vip"]["interface"]],
                  check=False).returncode:
            problems.append(f"{name}: missing VIP interface")
        disk = config["drbd"]["backing_device"]
        if remote(node, ["test", "-b", disk], check=False).returncode:
            problems.append(f"{name}: backing device is not a block device")
        mount = remote(node, ["findmnt", "--source", disk], check=False)
        if mount.returncode == 0:
            problems.append(f"{name}: backing device is mounted")
        elif mount.returncode != 1 or (mount.stderr or "").strip():
            problems.append(f"{name}: cannot verify backing-device mount state")
        needed = ("pcs", "crm_mon", "drbdadm", "drbdsetup", "smbd", "crm_verify", "findmnt",
                  config["fencing"]["agent"])
        if config["fencing"]["agent"] == "fence_ipmilan":
            needed += ("ipmitool",)
        for command in needed:
            if not facts["commands"].get(command):
                problems.append(f"{name}: missing {command}; install supported packages first")
        for agent, available in facts["ocf"].items():
            if not available:
                problems.append(f"{name}: missing OCF agent {agent}")
        if not facts["drbd9"]:
            problems.append(f"{name}: loaded DRBD kernel module is not version 9")
        if any(facts[key] for key in ("legacy_active", "samba_active", "legacy_enabled",
                                       "samba_enabled", "drbd_enabled")):
            problems.append(f"{name}: independent Saturn/Samba/DRBD services are active or enabled")
        if fresh and (facts["cluster_exists"] or facts["marker_exists"] or facts["config_exists"]):
            problems.append(f"{name}: existing HA configuration; do not overwrite or re-run prepare")
        if fresh:
            paths = ["/etc/drbd.d/" + config["drbd"]["resource"] + ".res"]
            paths += [config["fencing"]["password_script_dir"] + "/" + peer["name"]
                      for peer in config["nodes"]]
            paths += ["/etc/saturn/fencing/" + peer["name"] + ".password"
                      for peer in config["nodes"]]
            for path in paths:
                if remote(node, ["test", "-e", path], check=False).returncode == 0:
                    problems.append(f"{name}: {path} already exists")
        if not fresh and (not facts["marker_exists"] or not facts["config_exists"]):
            problems.append(f"{name}: deployment is incomplete")
    if problems:
        raise InstallationError("Prechecks failed:\n  - " + "\n  - ".join(problems))


def check_ssh_mesh(config):
    banner("SSH trust", "Checking strict host keys and passwordless root access in all directions.")
    for source in config["nodes"]:
        for target in config["nodes"]:
            if source is target:
                continue
            nested = ["ssh", *SSH_OPTIONS, "root@" + target["cluster_ip"], "true"]
            remote(source, nested, timeout=22)
            print(f"  ✓ {source['name']} → {target['name']}")


def write_remote(node, path, content, mode):
    # noclobber prevents overwriting credentials, DRBD configurations and data.
    script = "set -e; set -C; umask 077; cat > " + shlex.quote(path) + "; chmod " + oct(mode)[2:] + " " + shlex.quote(path)
    remote(node, ["sh", "-c", script], input_data=content)


def prepare(config):
    banner("Read-only prechecks", "Existing resources are never overwritten.")
    inspect_nodes(config, fresh=True)
    check_ssh_mesh(config)
    print("\n  Confirm backups, empty/new cluster state, disabled independent services,")
    print("  and compatible supported DRBD 9 packages on EVERY node.")
    confirm("PREPARE THREE NODES")
    secrets = {}
    for node in config["nodes"]:
        password = getpass.getpass(f"  BMC password for {node['name']} (not saved locally): ")
        if not password or "\n" in password or "\r" in password:
            raise InstallationError("BMC password must be nonempty and single-line")
        secrets[node["name"]] = password + "\n"
    drbd_file = "/etc/drbd.d/" + config["drbd"]["resource"] + ".res"
    for node in config["nodes"]:
        name = node["name"]
        remote(node, ["install", "-d", "-m", "0700", "/etc/saturn", "/etc/saturn/fencing",
                      config["fencing"]["password_script_dir"]])
        write_remote(node, CLUSTER_CONFIG, json.dumps(config, indent=2) + "\n", 0o600)
        write_remote(node, drbd_file, render.render_drbd(config), 0o600)
        for target, script in render.render_secrets(config).items():
            write_remote(node, config["fencing"]["password_script_dir"] + "/" + target, script, 0o700)
            write_remote(node, "/etc/saturn/fencing/" + target + ".password", secrets[target], 0o600)
        # Parse but do not create metadata, bring up DRBD, format or mount.
        remote(node, ["drbdadm", "dump", config["drbd"]["resource"]])
        write_remote(node, MARKER, "Pacemaker owns DRBD, Samba, and the VIP.\n", 0o600)
        print(f"  ✓ Configuration staged on {name}")
    preflight_all(config)
    verify_deployment(config)
    banner("Prepared, not activated")
    print("  Re-run /etc/saturn/preflight.py on each node as needed. Set a")
    print("  hacluster password, start pcsd, then run 'pcs host auth' on this node")
    print("  (enter its password interactively). Do not put passwords in argv.")
    print("  Return with: python3 ha/install.py bootstrap --config <private.json>")


def preflight_all(config):
    # Preflight's source travels via stdin; no repository checkout on peers.
    source = (Path(__file__).with_name("render.py").read_text(),
              Path(__file__).with_name("preflight.py").read_text())
    for node in config["nodes"]:
        for filename, contents in zip(("render.py", "preflight.py"), source):
            # Install source once; refuse a partial install with stale code.
            path = "/etc/saturn/" + filename
            if remote(node, ["test", "-e", path], check=False).returncode:
                write_remote(node, path, contents, 0o600)
            digest = remote(node, ["sha256sum", path]).stdout.split()[0]
            if digest != hashlib.sha256(contents.encode()).hexdigest():
                raise InstallationError(f"{node['name']}: installed {filename} differs from this version")
        result = remote(node, ["python3", "/etc/saturn/preflight.py", CLUSTER_CONFIG], check=False)
        if result.returncode:
            raise InstallationError(f"{node['name']} preflight failed: {result.stdout} {result.stderr}")


def verify_deployment(config):
    expected = {CLUSTER_CONFIG: json.dumps(config, indent=2) + "\n",
                "/etc/drbd.d/" + config["drbd"]["resource"] + ".res": render.render_drbd(config)}
    for name, contents in render.render_secrets(config).items():
        expected[config["fencing"]["password_script_dir"] + "/" + name] = contents
    for node in config["nodes"]:
        for path, contents in expected.items():
            digest = remote(node, ["sha256sum", path]).stdout.split()[0]
            if digest != hashlib.sha256(contents.encode()).hexdigest():
                raise InstallationError(f"{node['name']}: {path} differs from the reviewed configuration")


def bootstrap(config):
    banner("Cluster bootstrap", "This phase creates Corosync/Pacemaker and BMC resources.")
    inspect_nodes(config, fresh=False)
    preflight_all(config)
    verify_deployment(config)
    check_ssh_mesh(config)
    for node in config["nodes"]:
        if remote(node, ["test", "-e", "/etc/corosync/corosync.conf"], check=False).returncode == 0:
            raise InstallationError("Existing Corosync config: refusing to replace an existing cluster")
    print("  pcs host auth must have completed successfully before this step.")
    confirm("CREATE THREE NODE CLUSTER")
    args = ["pcs", "cluster", "setup", config.get("cluster_name", "saturn")]
    for node in config["nodes"]:
        args += [node["name"], "addr=" + node["cluster_ip"]]
    run(args + ["--start", "--wait=60", "--enable"], timeout=180)
    membership_ready(config)
    # No DRBD/Samba/VIP resources exist yet. Never relax fencing or quorum.
    run(["pcs", "property", "set", "stonith-enabled=true", "no-quorum-policy=stop",
         "stonith-timeout=120s"])
    for line in render.render_plan(config).splitlines():
        if line.startswith("pcs stonith create ") or line.startswith("pcs constraint location fence_"):
            run(shlex.split(line), timeout=90)
    print("  ✓ Cluster and three fencing resources configured.")
    print("  STOP: test each real BMC fence with 'pcs stonith fence <node>'")
    print("  during maintenance. Confirm power-off and safely rejoin every node.")
    print("  Independently initialize/synchronize DRBD, format ext4 only on the")
    print("  chosen initial Primary, and complete backup/restore preparations.")
    print("  Return with: python3 ha/install.py resources --config <private.json>")


def membership_ready(config):
    xml = ET.fromstring(run(["crm_mon", "--output-as", "xml"], timeout=20).stdout)
    status = xml.find("./status")
    dc = xml.find("./summary/current_dc")
    online = {node.get("name") for node in xml.findall("./nodes/node") if node.get("online") == "true"}
    expected = {node["name"] for node in config["nodes"]}
    if (status is None or status.get("code") != "0" or dc is None or
            dc.get("with_quorum") != "true" or online != expected or
            any(node.get("unclean") == "true" for node in xml.findall("./nodes/node"))):
        raise InstallationError("Three online, clean nodes with quorum are required")
    return xml


def cluster_ready(config):
    xml = membership_ready(config)
    expected = {node["name"] for node in config["nodes"]}
    options = xml.find("./summary/cluster_options")
    if (options is None or options.get("stonith-enabled") != "true" or
            options.get("no-quorum-policy") != "stop" or options.get("maintenance-mode") != "false"):
        raise InstallationError("Strict fencing and quorum properties are required")
    if xml.find("./resources/clone") is not None or xml.find("./resources/group") is not None:
        raise InstallationError("Service resources already exist; refusing to overwrite them")
    fence = {item.get("id"): item.get("resource_agent") for item in xml.findall(".//resources//resource")}
    if set(fence) != {"fence_" + name for name in expected}:
        raise InstallationError("Unexpected resources in cluster; refusing to push a CIB")
    if not all(fence.get("fence_" + name) == "stonith:" + config["fencing"]["agent"] for name in expected):
        raise InstallationError("Missing fence resource; inspect pcs status before continuing")


def storage_ready(config):
    resource = config["drbd"]["resource"]
    for index, node in enumerate(config["nodes"]):
        mount = remote(node, ["findmnt", "--mountpoint", config["filesystem"]["mount"]],
                       check=False)
        if mount.returncode == 0:
            raise InstallationError(f"{node['name']}: share is already mounted outside Pacemaker")
        if mount.returncode != 1 or (mount.stderr or "").strip():
            raise InstallationError(f"{node['name']}: cannot verify share mount state")
        result = remote(node, ["drbdsetup", "status", resource, "--json"])
        state = json.loads(result.stdout)
        if not isinstance(state, list) or len(state) != 1:
            raise InstallationError(f"{node['name']}: missing DRBD resource")
        for item in state:
            if (item.get("name") != resource or item.get("node-id") != index or
                    item.get("role") != "Secondary" or
                    item.get("suspended") is not False or len(item.get("devices", [])) != 1):
                raise InstallationError(f"{node['name']}: unexpected DRBD state")
            if any(device.get("volume") != 0 or device.get("disk-state") != "UpToDate" or
                   device.get("quorum") is not True
                   for device in item["devices"]):
                raise InstallationError(f"{node['name']}: DRBD disk not UpToDate")
            connections = item.get("connections", [])
            if (len(connections) != 2 or
                    {peer.get("peer-node-id") for peer in connections} != {0, 1, 2} - {index} or
                    any(peer.get("connection-state") != "Connected" for peer in connections)):
                raise InstallationError(f"{node['name']}: not connected to both peers")
            for peer in connections:
                volumes = peer.get("peer_devices", [])
                if len(volumes) != 1 or any(volume.get("volume") != 0 or
                                            volume.get("peer-disk-state") != "UpToDate" or
                                            volume.get("replication-state") != "Established" or
                                            volume.get("out-of-sync") != 0 for volume in volumes):
                    raise InstallationError(f"{node['name']}: peer disk not UpToDate")


def resources(config):
    banner("Service activation", "No untested BMC or unsynchronized DRBD may pass this gate.")
    inspect_nodes(config, fresh=False)
    preflight_all(config)
    verify_deployment(config)
    cluster_ready(config)
    storage_ready(config)
    print("  Inspect pcs stonith history and physical BMC event logs first.")
    print("  Your confirmation is NOT a substitute for the real fence tests.")
    confirm("ALL THREE BMC FENCES TESTED")
    confirm("BACKUP RESTORE AND DRBD VERIFIED")
    with tempfile.TemporaryDirectory(prefix="saturn-cib-", dir="/root") as directory:
        cib = str(Path(directory) / "cluster.cib")
        baseline = str(Path(directory) / "original.cib")
        run(["pcs", "cluster", "cib", cib])
        Path(baseline).write_bytes(Path(cib).read_bytes())
        for line in render.render_plan(config).splitlines():
            if not line.startswith("pcs -f saturn.cib "):
                continue
            args = shlex.split(line)
            args[2] = cib
            run(args, timeout=90)
        run(["crm_verify", "-x", cib, "-V"])
        # Recheck immediately before pushing. Abort if another actor changed CIB.
        cluster_ready(config)
        current = str(Path(directory) / "current.cib")
        run(["pcs", "cluster", "cib", current])
        def configuration(path):
            element = ET.fromstring(Path(path).read_bytes()).find("configuration")
            if element is None:
                raise InstallationError("CIB has no configuration section")
            return ET.tostring(element)
        if configuration(current) != configuration(baseline):
            raise InstallationError("Cluster CIB changed during review; aborting service activation")
        run(["pcs", "cluster", "cib-push", cib, "diff-against=" + baseline], timeout=90)
    print("  ✓ Service resources submitted. Check pcs status and complete")
    print("  ha/ACCEPTANCE.md on this hardware before production use.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("wizard", "prepare", "bootstrap", "resources"), nargs="?", default="wizard")
    parser.add_argument("--config", type=Path, help="Private 0600 JSON configuration, outside the repository")
    args = parser.parse_args()
    try:
        if args.stage == "wizard":
            if os.geteuid() != 0 or not sys.stdin.isatty() or not args.config:
                raise InstallationError("Run as root interactively with --config /root/saturn-cluster.json")
            config = wizard()
            review(config)
            confirm("SAVE CONFIG")
            save_config(args.config, config)
            print(f"  Private configuration saved at {args.config}")
            print("  Review with: python3 ha/render.py <config> <new-plan-directory>")
            print("  Next: python3 ha/install.py prepare --config <config>")
        else:
            if os.geteuid() != 0 or not sys.stdin.isatty() or not args.config:
                raise InstallationError("Stages require root, an interactive terminal and --config")
            config = load_config(args.config)
            {"prepare": prepare, "bootstrap": bootstrap, "resources": resources}[args.stage](config)
    except (InstallationError, ValueError, KeyError, OSError, json.JSONDecodeError,
            subprocess.TimeoutExpired, ET.ParseError) as exc:
        parser.exit(1, f"Stopped safely: {exc}\nInspect state before retrying; no automatic rollback.\n")


if __name__ == "__main__":
    main()
