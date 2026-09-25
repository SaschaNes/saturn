#!/usr/bin/env python3
"""Versioned off-cluster backups and explicitly offline restores for Saturn."""

import argparse
import logging
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from saturn import Etcd, EtcdError, load_config


SSH = "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=5"
PERIODS = ("daily", "weekly", "monthly")


def snapshots(root, period):
    return sorted(path for path in (root / period).iterdir()
                  if path.is_dir() and re.fullmatch(r"\d{8}T\d{6}Z", path.name))


def run_rsync(source, destination):
    subprocess.run(["rsync", "-aAX", "--numeric-ids", "--delete", "--timeout=30",
                    "-e", SSH, "--", source, destination], check=True)


def backup(config, etcd, root, period, retain):
    owner = etcd.get("owner")
    if not owner:
        raise RuntimeError("No active owner; refusing backup")
    node = owner.split(":", 1)[0]
    if node not in config["nodes"]:
        raise RuntimeError("Unknown owner")
    directory = root / period
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = directory / now
    if destination.exists():
        raise RuntimeError("Backup already exists at this timestamp")
    staging = directory / (".bk-" + now)
    if staging.exists():
        raise RuntimeError("Staging directory already exists; investigate it first")
    staging.mkdir()
    source = "root@" + config["nodes"][node] + ":" + config["data_dir"].rstrip("/") + "/"
    try:
        run_rsync(source, str(staging) + "/")
        if etcd.get("owner") != owner:
            raise RuntimeError("Leader changed during backup; discarding incomplete snapshot")
        staging.rename(destination)
    except Exception:
        shutil.rmtree(staging)
        raise
    print("Backup created:", destination)
    for old in snapshots(root, period)[:-retain]:
        shutil.rmtree(old)


def restore(config, etcd, root, period, snapshot, target):
    if etcd.get("owner") is not None or etcd.get("approval") is not None:
        raise RuntimeError("Cluster is active or takeover approved; stop services and clear approval first")
    if target not in config["nodes"]:
        raise ValueError("Unknown target node")
    if not re.fullmatch(r"\d{8}T\d{6}Z", snapshot):
        raise ValueError("Invalid snapshot name")
    source = root / period / snapshot
    if not source.is_dir():
        raise ValueError("Snapshot not found")
    destination = "root@" + config["nodes"][target] + ":" + config["data_dir"].rstrip("/") + "/"
    run_rsync(str(source) + "/", destination)
    print("Restored to", target, "; verify data, then explicitly approve this node")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/saturn/config.json")
    parser.add_argument("--backup-dir", default="/var/backups/saturn")
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("backup")
    create.add_argument("period", choices=PERIODS)
    create.add_argument("--retain", type=int, default=7, help="Number of complete snapshots to retain")
    recovery = sub.add_parser("restore")
    recovery.add_argument("period", choices=PERIODS)
    recovery.add_argument("snapshot", help="Snapshot directory name (UTC timestamp)")
    recovery.add_argument("--target", required=True, help="Node to receive the files")
    recovery.add_argument("--services-stopped", action="store_true", required=True,
                          help="Confirm Saturn/Samba are stopped on all nodes and clients are disconnected")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        config = load_config(args.config)
        etcd = Etcd(config)
        root = Path(args.backup_dir).resolve()
        if root == Path("/") or root.is_relative_to(Path(config["data_dir"]).resolve()):
            raise ValueError("Backups must not be stored inside the source directory")
        if args.action == "backup":
            if args.retain < 1:
                raise ValueError("--retain must be positive")
            backup(config, etcd, root, args.period, args.retain)
        else:
            restore(config, etcd, root, args.period, args.snapshot, args.target)
    except (OSError, ValueError, EtcdError, RuntimeError, subprocess.SubprocessError) as exc:
        logging.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
