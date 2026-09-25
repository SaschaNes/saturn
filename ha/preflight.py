#!/usr/bin/env python3
"""Read-only local prerequisites for the Pacemaker/DRBD migration."""

import argparse
import json
import shutil
import stat
import subprocess
from pathlib import Path

if __package__:
    from .render import validate
else:
    from render import validate


REQUIRED = ("pcs", "crm_mon", "drbdadm", "systemctl", "fence_ipmilan")


def check(config, root=Path("/"), which=shutil.which, run=subprocess.run):
    problems = []
    for name in REQUIRED:
        if not which(name):
            problems.append("Missing command: " + name)
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
        for unit in ("saturn.service", "smbd.service"):
            result = run(["systemctl", "is-enabled", unit], capture_output=True, text=True, timeout=5)
            allowed = ("disabled", "masked", "not-found") if unit == "saturn.service" else ("disabled",)
            if result.stdout.strip() not in allowed:
                problems.append(unit + " must not start outside Pacemaker")
        if run(["systemctl", "is-active", "--quiet", "saturn.service"], timeout=5).returncode == 0:
            problems.append("Legacy Saturn coordinator is still running")
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
