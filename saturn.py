#!/usr/bin/env python3
"""Fail-closed Samba VIP coordinator. Requires explicit approval for every takeover."""

import argparse
import base64
import ipaddress
import json
import logging
import os
import re
import signal
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


LOG = logging.getLogger("saturn")
SAFE_PATH = re.compile(r"^/[a-zA-Z0-9_./-]+$")


def encoded(value):
    return base64.b64encode(value.encode()).decode()


def decoded(value):
    return base64.b64decode(value, validate=True).decode()


def load_config(path):
    config = json.loads(Path(path).read_text())
    required = ("node", "nodes", "endpoints", "vip", "interface", "data_dir")
    if any(not config.get(key) for key in required):
        raise ValueError("Missing configuration: " + ", ".join(k for k in required if not config.get(k)))
    if config["node"] not in config["nodes"] or len(config["nodes"]) != 3:
        raise ValueError("Specify this node and exactly three distinct nodes")
    if any(not re.fullmatch(r"[a-zA-Z0-9_-]+", name) for name in config["nodes"]):
        raise ValueError("Invalid node name")
    for address in config["nodes"].values():
        if not isinstance(ipaddress.ip_address(address), ipaddress.IPv4Address):
            raise ValueError("Only IPv4 node addresses are supported")
    if not isinstance(ipaddress.ip_interface(config["vip"]), ipaddress.IPv4Interface):
        raise ValueError("Only IPv4 VIPs are supported")
    if not config["interface"].replace("-", "").replace("_", "").isalnum():
        raise ValueError("Invalid network interface")
    data_dir = config["data_dir"]
    if not SAFE_PATH.fullmatch(data_dir) or ".." in Path(data_dir).parts or data_dir == "/":
        raise ValueError("data_dir must be a safe absolute directory (not /)")
    if any(not endpoint.startswith(("https://", "http://")) for endpoint in config["endpoints"]):
        raise ValueError("endpoints must be HTTP(S) URLs for the etcd v3 gateway")
    config.setdefault("key_prefix", "/saturn/cluster")
    if not config["key_prefix"].startswith("/") or config["key_prefix"].endswith("/"):
        raise ValueError("key_prefix must begin with / and not end with /")
    config.setdefault("lease_ttl", 30)
    config.setdefault("poll_interval", 3)
    config.setdefault("sync_interval", 30)
    config.setdefault("require_mount", True)
    if not 15 <= config["lease_ttl"] <= 300 or not 1 <= config["poll_interval"] <= config["lease_ttl"] / 5:
        raise ValueError("Invalid lease TTL or polling interval")
    if config["sync_interval"] < 1:
        raise ValueError("Invalid sync interval")
    if not isinstance(config["require_mount"], bool):
        raise ValueError("require_mount must be a boolean")
    return config


class EtcdError(RuntimeError):
    pass


class Etcd:
    def __init__(self, config):
        self.endpoints = config["endpoints"]
        self.prefix = config["key_prefix"]
        self.context = ssl.create_default_context(cafile=config.get("ca_file"))
        if config.get("client_cert"):
            self.context.load_cert_chain(config["client_cert"], config.get("client_key"))

    def request(self, path, body):
        errors = []
        deadline = time.monotonic() + 4
        for endpoint in self.endpoints:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                request = urllib.request.Request(
                    endpoint.rstrip("/") + path,
                    json.dumps(body).encode(),
                    {"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(request, timeout=min(2, remaining), context=self.context) as response:
                    result = json.load(response)
                if "error" in result:
                    raise EtcdError(str(result["error"]))
                return result
            except (OSError, ValueError, EtcdError) as exc:
                errors.append(str(exc))
        raise EtcdError("etcd unavailable: " + "; ".join(errors))

    def key(self, suffix):
        return encoded(self.prefix + "/" + suffix)

    def get(self, suffix):
        values = self.request("/v3/kv/range", {"key": self.key(suffix)}).get("kvs", [])
        return decoded(values[0]["value"]) if values else None

    def txn(self, compares, success):
        return self.request("/v3/kv/txn", {"compare": compares, "success": success, "failure": []})["succeeded"]

    def compare_value(self, suffix, value):
        if value is None:
            return {"key": self.key(suffix), "target": "VERSION", "result": "EQUAL", "version": "0"}
        return {"key": self.key(suffix), "target": "VALUE", "result": "EQUAL", "value": encoded(value)}

    def put(self, suffix, value, lease=None):
        fields = {"key": self.key(suffix), "value": encoded(value)}
        if lease is not None:
            fields["lease"] = str(lease)
        return {"request_put": fields}

    def delete(self, suffix):
        return {"request_delete_range": {"key": self.key(suffix)}}

    def grant(self, ttl):
        return int(self.request("/v3/lease/grant", {"TTL": ttl})["ID"])

    def revoke(self, lease):
        self.request("/v3/lease/revoke", {"ID": str(lease)})

    def approve(self, node):
        # An approval applies only to an unlocked cluster at a particular last-owner state.
        previous = self.get("last_owner")
        approval = json.dumps({"node": node, "previous": previous, "id": uuid.uuid4().hex})
        ok = self.txn(
            [self.compare_value("owner", None), self.compare_value("approval", None),
             self.compare_value("last_owner", previous)],
            [self.put("approval", approval)],
        )
        if not ok:
            raise EtcdError("Owner or approval changed; inspect status before approving")

    def cancel_approval(self):
        approval = self.get("approval")
        if approval is None:
            return
        if not self.txn([self.compare_value("approval", approval)], [self.delete("approval")]):
            raise EtcdError("Approval changed; inspect status before cancelling")

    def acquire(self, node, approval):
        try:
            record = json.loads(approval)
            if record["node"] != node:
                return None
            previous = record["previous"]
        except (KeyError, ValueError, TypeError):
            raise EtcdError("Invalid approval record")
        token = node + ":" + uuid.uuid4().hex
        lease = self.grant(self.ttl)
        ok = False
        try:
            ok = self.txn(
                [self.compare_value("owner", None), self.compare_value("approval", approval),
                 self.compare_value("last_owner", previous)],
                [self.put("owner", token, lease), self.put("last_owner", node), self.delete("approval")],
            )
            return (token, lease) if ok else None
        finally:
            if not ok:
                try:
                    self.revoke(lease)
                except EtcdError:
                    LOG.warning("Could not revoke unused lease", exc_info=True)

    def renew(self, token, old_lease):
        # Replace the lease atomically rather than relying on a long-lived keepalive stream.
        new_lease = self.grant(self.ttl)
        try:
            ok = self.txn([self.compare_value("owner", token)], [self.put("owner", token, new_lease)])
            if not ok:
                raise EtcdError("Ownership lost")
        except Exception:
            try:
                self.revoke(new_lease)
            except EtcdError:
                LOG.warning("Could not revoke unused lease", exc_info=True)
            raise
        try:
            self.revoke(old_lease)
        except EtcdError:
            LOG.warning("Old lease will expire on its own", exc_info=True)
        return new_lease

    def release(self, token, lease):
        # Never remove another owner's key after losing our lease.
        if self.txn([self.compare_value("owner", token)], [self.delete("owner")]):
            self.revoke(lease)


def command(*args, timeout=10):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout)


class Coordinator:
    def __init__(self, config, etcd):
        self.config = config
        self.etcd = etcd
        self.etcd.ttl = config["lease_ttl"]
        self.ownership = None
        self.running = True
        self.sync_stop = threading.Event()
        self.sync_thread = None
        self.sync_process = None

    def check_data(self):
        if not Path(self.config["data_dir"]).is_dir():
            raise RuntimeError("Data directory missing")
        if self.config["require_mount"] and not os.path.ismount(self.config["data_dir"]):
            raise RuntimeError("Data directory is not a mounted filesystem")

    def vip_present(self):
        result = command("ip", "-j", "address", "show", "dev", self.config["interface"], timeout=3)
        vip = ipaddress.ip_interface(self.config["vip"]).ip
        return any(ipaddress.ip_address(addr["local"]) == vip
                   for iface in json.loads(result.stdout) for addr in iface.get("addr_info", []))

    def demote(self):
        self.sync_stop.set()
        if self.sync_process and self.sync_process.poll() is None:
            try:
                self.sync_process.terminate()
            except ProcessLookupError:
                pass  # rsync exited between poll() and terminate().
        if self.sync_thread:
            self.sync_thread.join(timeout=5)
        failures = []
        if self.sync_thread and self.sync_thread.is_alive():
            failures.append("replication process did not stop")
        try:
            command("systemctl", "stop", "smbd.service")
        except (OSError, subprocess.SubprocessError) as exc:
            failures.append(str(exc))
        try:
            if self.vip_present():
                command("ip", "address", "del", self.config["vip"], "dev", self.config["interface"])
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            failures.append(str(exc))
        if failures:
            raise RuntimeError("Demotion failed; manually isolate this host: " + "; ".join(failures))
        LOG.info("Samba stopped and VIP removed")

    def promote(self):
        self.check_data()
        if not self.vip_present():
            command("ip", "address", "add", self.config["vip"], "dev", self.config["interface"])
        command("systemctl", "start", "smbd.service")
        command("systemctl", "is-active", "--quiet", "smbd.service")
        LOG.info("Promoted %s", self.config["node"])

    def sync(self):
        source = self.config["data_dir"].rstrip("/") + "/"
        for node, address in self.config["nodes"].items():
            if node == self.config["node"] or self.sync_stop.is_set():
                continue
            try:
                self.check_data()
            except RuntimeError:
                LOG.exception("Replication stopped: source data unavailable")
                return
            destination = "root@" + address + ":" + source
            args = ["rsync", "-aAX", "--numeric-ids", "--delete", "--timeout=20",
                    "-e", "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=5",
                    "--", source, destination]
            try:
                self.sync_process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                while self.sync_process.poll() is None:
                    if self.sync_stop.wait(0.2):
                        self.sync_process.terminate()
                        break
                try:
                    self.sync_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.sync_process.kill()
                    self.sync_process.wait()
                if self.sync_process.returncode != 0 and not self.sync_stop.is_set():
                    LOG.error("Replication to %s failed (exit %s)", node, self.sync_process.returncode)
                elif not self.sync_stop.is_set():
                    LOG.info("Replication to %s completed", node)
            except (OSError, subprocess.SubprocessError):
                LOG.exception("Replication to %s failed", node)
            finally:
                self.sync_process = None

    def run(self):
        self.demote()  # On every start, remove any stale local VIP before consulting etcd.
        next_sync = 0
        while self.running:
            try:
                if self.ownership:
                    token, lease = self.ownership
                    self.ownership = (token, self.etcd.renew(token, lease))
                    self.check_data()
                    if not self.vip_present():
                        raise RuntimeError("Active VIP is missing")
                    command("systemctl", "is-active", "--quiet", "smbd.service", timeout=3)
                    if time.monotonic() >= next_sync and not (self.sync_thread and self.sync_thread.is_alive()):
                        self.sync_stop.clear()
                        self.sync_thread = threading.Thread(target=self.sync, daemon=True)
                        self.sync_thread.start()
                        next_sync = time.monotonic() + self.config["sync_interval"]
                else:
                    approval = self.etcd.get("approval")
                    if approval:
                        ownership = self.etcd.acquire(self.config["node"], approval)
                        if ownership:
                            self.ownership = ownership
                            self.promote()
            except (EtcdError, OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                LOG.exception("Coordination failed; demoting")
                try:
                    self.demote()
                except Exception:
                    LOG.critical("Cannot safely demote; operator must fence this node", exc_info=True)
                    return 1
                if self.ownership:
                    try:
                        self.etcd.release(*self.ownership)
                    except EtcdError:
                        LOG.warning("Lease will expire; manual approval still required", exc_info=True)
                    self.ownership = None
            time.sleep(self.config["poll_interval"])
        self.demote()
        if self.ownership:
            try:
                self.etcd.release(*self.ownership)
            except EtcdError:
                LOG.warning("Lease will expire; manual approval still required", exc_info=True)
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/saturn/config.json")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("run")
    sub.add_parser("status")
    sub.add_parser("cancel-approval", help="Remove a pending, unconsumed approval")
    approval = sub.add_parser("approve", help="One-shot manual approval for this node")
    approval.add_argument("--old-leader-fenced", action="store_true", required=True)
    approval.add_argument("--data-verified", action="store_true", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config(args.config)
        etcd = Etcd(config)
        if args.action == "status":
            for name in ("owner", "last_owner", "approval"):
                print(name + ": " + str(etcd.get(name)))
        elif args.action == "cancel-approval":
            etcd.cancel_approval()
            print("Pending approval cancelled")
        elif args.action == "approve":
            etcd.approve(config["node"])
            print("Approved a single takeover by " + config["node"])
        else:
            coordinator = Coordinator(config, etcd)
            def stop(_signum, _frame):
                coordinator.running = False
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            return coordinator.run()
    except (OSError, ValueError, EtcdError, RuntimeError) as exc:
        LOG.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
