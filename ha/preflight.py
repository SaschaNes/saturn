#!/usr/bin/env python3
"""Read-only local prerequisites for the Pacemaker/DRBD migration."""

import argparse
import ipaddress
import json
import shutil
import stat
import subprocess
from pathlib import Path

if __package__:
    from .render import validate
else:
    from render import validate


REQUIRED = ("pcs", "crm_mon", "drbdadm", "systemctl", "ip")


def check(config, root=Path("/"), which=shutil.which, run=subprocess.run):
    problems = []
    agent = config["fencing"]["agent"]
    required = REQUIRED + (agent,) + (("ipmitool",) if agent == "fence_ipmilan" else ())
    for name in required:
        if not which(name):
            problems.append("Missing command: " + name)
    if which("pcs") and which(agent):
        metadata = run(["pcs", "stonith", "describe", agent, "--full"],
                       capture_output=True, text=True, timeout=10)
        parameters = {line.strip().split()[0] for line in metadata.stdout.splitlines()
                      if line.startswith("  ") and line.strip()}
        specific = {"lanplus", "method"} if agent == "fence_ipmilan" else {"systems_uri", "ssl_secure"}
        missing = ({"ip", "username", "password_script"} | specific) - parameters
        if metadata.returncode or missing:
            problems.append("Installed " + agent + " lacks required fencing parameters: " + ", ".join(sorted(missing)))
    for name in ("linbit/drbd", "heartbeat/Filesystem", "heartbeat/IPaddr2"):
        agent = root / "usr/lib/ocf/resource.d" / name
        if not agent.is_file():
            problems.append("Missing OCF agent: " + str(agent))
    if not (root / "etc/saturn/pacemaker-managed").is_file():
        problems.append("Install /etc/saturn/pacemaker-managed before HA migration")
    drbd_version = root / "proc/drbd"
    if not drbd_version.is_file() or not drbd_version.read_text().startswith("version: 9."):
        problems.append("Loaded kernel module is not DRBD 9")
    for node in config["nodes"]:
        name = node["name"]
        script = root / config["fencing"]["password_script_dir"].lstrip("/") / name
        secret = root / "etc/saturn/fencing" / (name + ".password")
        for path, expected_mode in ((script, 0o700), (secret, 0o600)):
            try:
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != expected_mode:
                    problems.append(str(path) + " must be a root-owned regular file mode " + oct(expected_mode))
            except OSError:
                problems.append("Missing fencing credential component: " + str(path))
    if which("systemctl"):
        for unit in ("saturn.service", "smbd.service", "drbd.service"):
            result = run(["systemctl", "is-enabled", unit], capture_output=True, text=True, timeout=5)
            allowed = ("disabled", "masked", "not-found") if unit == "saturn.service" else ("disabled",)
            if unit == "drbd.service":
                allowed = ("disabled", "masked", "not-found", "static")
            if result.stdout.strip() not in allowed:
                problems.append(unit + " must not start outside Pacemaker")
        if run(["systemctl", "is-active", "--quiet", "saturn.service"], timeout=5).returncode == 0:
            problems.append("Legacy Saturn coordinator is still running")
        if run(["systemctl", "is-active", "--quiet", "smbd.service"], timeout=5).returncode == 0:
            problems.append("Samba is already running outside Pacemaker")
    if which("ip"):
        result = run(["ip", "-j", "address", "show", "dev", config["vip"]["interface"]],
                     capture_output=True, text=True, timeout=5)
        if result.returncode:
            problems.append("Cannot verify the configured VIP interface")
        else:
            vip = ipaddress.ip_interface(config["vip"]["address"]).ip
            for interface in json.loads(result.stdout):
                if any(ipaddress.ip_address(entry["local"]) == vip for entry in interface.get("addr_info", [])):
                    problems.append("VIP is already assigned outside Pacemaker")
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    try:
        config = validate(json.loads(args.config.read_text()))
        problems = check(config)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        parser.exit(1, "Preflight failed: " + str(exc) + "\n")
    print(json.dumps({"ready_for_review": not problems, "problems": problems}, indent=2))
    if problems:
        parser.exit(1)


if __name__ == "__main__":
    main()
