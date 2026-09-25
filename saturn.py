#!/usr/bin/env python3
"""Fail-closed Samba VIP coordinator. Requires explicit approval for every takeover."""

import argparse
import base64
from datetime import datetime, timezone
import http.server
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
    config.setdefault("lease_ttl", 60)
    config.setdefault("poll_interval", 3)
    config.setdefault("sync_interval", 30)
    config.setdefault("require_mount", True)
    config.setdefault("api_listen", "127.0.0.1")
    config.setdefault("api_port", 8008)
    if not 30 <= config["lease_ttl"] <= 300 or not 1 <= config["poll_interval"] <= min(5, config["lease_ttl"] / 10):
        raise ValueError("Invalid lease TTL or polling interval")
    if config["sync_interval"] < 1:
        raise ValueError("Invalid sync interval")
    if not isinstance(config["require_mount"], bool):
        raise ValueError("require_mount must be a boolean")
    if config["api_listen"] != "127.0.0.1" or not 1 <= config["api_port"] <= 65535:
        raise ValueError("Status API must bind to 127.0.0.1 and a valid port")
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

    def request_switchover(self, node, target):
        owner = self.get("owner")
        if not owner or owner.split(":", 1)[0] != node or target == node:
            raise EtcdError("Switchover must be requested from the active node to another node")
        request = json.dumps({"owner": owner, "target": target, "id": uuid.uuid4().hex})
        if not self.txn([self.compare_value("owner", owner), self.compare_value("switchover", None),
                         self.compare_value("approval", None)], [self.put("switchover", request)]):
            raise EtcdError("Cluster state changed; inspect status before retrying")

    def handoff(self, token, request, target, lease):
        approval = json.dumps({"node": target, "previous": token.split(":", 1)[0], "id": uuid.uuid4().hex})
        if not self.txn([self.compare_value("owner", token), self.compare_value("switchover", request),
                         self.compare_value("approval", None)],
                        [self.delete("owner"), self.delete("switchover"), self.put("approval", approval)]):
            raise EtcdError("Switchover state changed; no promotion approved")
        try:
            self.revoke(lease)
        except EtcdError:
            LOG.warning("Old lease will expire on its own", exc_info=True)

    def cancel_switchover(self, request):
        if request:
            self.txn([self.compare_value("switchover", request)], [self.delete("switchover")])

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


def command(*args, timeout=10, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True, timeout=timeout)


class StatusHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/status", "/health", "/primary", "/standby"):
            self.send_error(404)
            return
        status = self.server.coordinator.status()
        healthy = status["dcs_healthy"]
        allowed = {
            "/status": True,
            "/health": healthy,
            "/primary": healthy and status["role"] == "primary",
            "/standby": healthy and status["role"] == "standby",
        }
        body = json.dumps(status).encode()
        self.send_response(200 if allowed[self.path] else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string, *args):
        LOG.debug("Status API: " + format_string, *args)


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
        self.role = "starting"
        self.last_dcs_ok = 0
        self.last_sync = {}
        self.state_lock = threading.Lock()
        self.handoff_request = None
        self.handoff_target = None
        self.handoff_thread = None
        self.handoff_result = None

    def set_role(self, role):
        with self.state_lock:
            self.role = role

    def note_dcs(self):
        with self.state_lock:
            self.last_dcs_ok = time.monotonic()

    def note_dcs_lost(self):
        with self.state_lock:
            self.last_dcs_ok = 0

    def status(self):
        with self.state_lock:
            healthy = time.monotonic() - self.last_dcs_ok < min(self.config["lease_ttl"] / 3,
                                                                self.config["poll_interval"] * 3)
            return {"node": self.config["node"], "role": self.role, "dcs_healthy": healthy,
                    "last_sync": dict(self.last_sync)}

    def serve_status(self):
        server = http.server.ThreadingHTTPServer((self.config["api_listen"], self.config["api_port"]), StatusHandler)
        server.daemon_threads = True
        server.coordinator = self
        threading.Thread(target=server.serve_forever, name="saturn-status", daemon=True).start()
        return server

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
        self.set_role("demoting")
        self.sync_stop.set()
        process = self.sync_process
        if process and process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass  # rsync exited between poll() and terminate().
        if self.sync_thread:
            self.sync_thread.join(timeout=5)
        if self.handoff_thread:
            self.handoff_thread.join(timeout=5)
        failures = []
        if self.sync_thread and self.sync_thread.is_alive():
            failures.append("replication process did not stop")
        if self.handoff_thread and self.handoff_thread.is_alive():
            failures.append("final synchronization did not stop")
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
            self.set_role("error")
            raise RuntimeError("Demotion failed; manually isolate this host: " + "; ".join(failures))
        self.set_role("standby")
        LOG.info("Samba stopped and VIP removed")

    def promote(self):
        self.check_data()
        if not self.vip_present():
            command("ip", "address", "add", self.config["vip"], "dev", self.config["interface"])
        command("systemctl", "start", "smbd.service")
        command("systemctl", "is-active", "--quiet", "smbd.service")
        self.set_role("primary")
        LOG.info("Promoted %s", self.config["node"])

    def replicate(self, node, address):
        source = self.config["data_dir"].rstrip("/") + "/"
        self.check_data()
        destination = "root@" + address + ":" + source
        args = ["rsync", "-aAX", "--numeric-ids", "--delete", "--timeout=20",
                "-e", "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=5",
                "--", source, destination]
        try:
            self.sync_process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            while self.sync_process.poll() is None:
                if self.sync_stop.wait(0.2):
                    try:
                        self.sync_process.terminate()
                    except ProcessLookupError:
                        pass
                    break
            try:
                self.sync_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.sync_process.kill()
                self.sync_process.wait()
            if self.sync_process.returncode == 0 and not self.sync_stop.is_set():
                with self.state_lock:
                    self.last_sync[node] = datetime.now(timezone.utc).isoformat()
                LOG.info("Replication to %s completed", node)
                return True
            if not self.sync_stop.is_set():
                LOG.error("Replication to %s failed (exit %s)", node, self.sync_process.returncode)
        except (OSError, subprocess.SubprocessError):
            LOG.exception("Replication to %s failed", node)
        finally:
            self.sync_process = None
        return False

    def sync(self):
        for node, address in self.config["nodes"].items():
            if node != self.config["node"] and not self.sync_stop.is_set():
                try:
                    self.replicate(node, address)
                except RuntimeError:
                    LOG.exception("Replication stopped: source data unavailable")

    def start_handoff(self, request):
        record = json.loads(request)
        token, _ = self.ownership
        target = record["target"]
        if record["owner"] != token or target not in self.config["nodes"] or target == self.config["node"]:
            raise RuntimeError("Invalid switchover request")
        LOG.info("Quiescing Samba for planned switchover to %s", target)
        self.demote()  # Stop all writes and background replication before final sync.
        self.check_data()
        self.handoff_request = request
        self.handoff_target = target
        self.handoff_result = None
        self.sync_stop.clear()
        self.set_role("switchover")
        self.handoff_thread = threading.Thread(target=self.final_sync, daemon=True)
        self.handoff_thread.start()

    def final_sync(self):
        target = self.handoff_target
        address = self.config["nodes"][target]
        host = "root@" + address
        ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ConnectTimeout=5", host]
        try:
            check = ["mountpoint", "-q", "--", self.config["data_dir"]] if self.config["require_mount"] else ["test", "-d", self.config["data_dir"]]
            subprocess.run(ssh + check, check=True, timeout=10)
            inactive = subprocess.run(ssh + ["systemctl", "is-active", "--quiet", "smbd.service"], timeout=10)
            if inactive.returncode != 3:
                raise RuntimeError("Target Samba is active or cannot be checked")
            addresses = subprocess.run(ssh + ["ip", "-j", "address", "show", "dev", self.config["interface"]],
                                       capture_output=True, text=True, check=True, timeout=10)
            vip = ipaddress.ip_interface(self.config["vip"]).ip
            if any(ipaddress.ip_address(addr["local"]) == vip for iface in json.loads(addresses.stdout)
                   for addr in iface.get("addr_info", [])):
                raise RuntimeError("Target already has the VIP")
            self.handoff_result = self.replicate(target, address)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            LOG.exception("Target preflight/final synchronization failed")
            self.handoff_result = False

    def run(self):
        self.demote()  # On every start, remove any stale local VIP before consulting etcd.
        server = self.serve_status()
        next_sync = 0
        try:
            while self.running:
                try:
                    if self.ownership:
                        token, lease = self.ownership
                        self.ownership = (token, self.etcd.renew(token, lease))
                        self.note_dcs()
                        self.check_data()
                        if self.handoff_request:
                            if not self.handoff_thread.is_alive():
                                if not self.handoff_result:
                                    raise RuntimeError("Final synchronization failed; no promotion approved")
                                self.etcd.handoff(token, self.handoff_request, self.handoff_target, self.ownership[1])
                                LOG.info("Switchover approved for %s", self.handoff_target)
                                self.ownership = None
                                self.handoff_request = None
                                self.set_role("standby")
                        else:
                            if not self.vip_present():
                                raise RuntimeError("Active VIP is missing")
                            command("systemctl", "is-active", "--quiet", "smbd.service", timeout=3)
                            request = self.etcd.get("switchover")
                            if request and json.loads(request).get("owner") == token:
                                self.start_handoff(request)
                            elif time.monotonic() >= next_sync and not (self.sync_thread and self.sync_thread.is_alive()):
                                self.sync_stop.clear()
                                self.sync_thread = threading.Thread(target=self.sync, daemon=True)
                                self.sync_thread.start()
                                next_sync = time.monotonic() + self.config["sync_interval"]
                    else:
                        approval = self.etcd.get("approval")
                        self.note_dcs()
                        self.check_data()
                        if self.vip_present():
                            raise RuntimeError("Unexpected VIP on a standby node")
                        active = command("systemctl", "is-active", "--quiet", "smbd.service", timeout=3, check=False)
                        if active.returncode == 0:
                            raise RuntimeError("Unexpected Samba on a standby node")
                        if active.returncode != 3:  # systemctl reports inactive as exit 3.
                            raise RuntimeError("Cannot verify inactive Samba on standby")
                        if approval:
                            ownership = self.etcd.acquire(self.config["node"], approval)
                            if ownership:
                                self.ownership = ownership
                                self.note_dcs()
                                self.promote()
                except (EtcdError, OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                    LOG.exception("Coordination failed; demoting")
                    self.note_dcs_lost()
                    try:
                        self.demote()
                    except Exception:
                        LOG.critical("Cannot safely demote; operator must fence this node", exc_info=True)
                        return 1
                    if self.handoff_request:
                        try:
                            self.etcd.cancel_switchover(self.handoff_request)
                        except EtcdError:
                            LOG.warning("Stale switchover request may need operator removal", exc_info=True)
                        self.handoff_request = None
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
        finally:
            server.shutdown()
            server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/saturn/config.json")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("run")
    sub.add_parser("status")
    sub.add_parser("cancel-approval", help="Remove a pending, unconsumed approval")
    sub.add_parser("cancel-switchover", help="Remove a pending switchover request")
    planned = sub.add_parser("switchover", help="Quiesce this primary and transfer to a synced target")
    planned.add_argument("--target", required=True)
    approval = sub.add_parser("approve", help="One-shot manual approval for this node")
    approval.add_argument("--old-leader-fenced", action="store_true", required=True)
    approval.add_argument("--data-verified", action="store_true", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config(args.config)
        etcd = Etcd(config)
        if args.action == "status":
            for name in ("owner", "last_owner", "approval", "switchover"):
                print(name + ": " + str(etcd.get(name)))
        elif args.action == "cancel-approval":
            etcd.cancel_approval()
            print("Pending approval cancelled")
        elif args.action == "cancel-switchover":
            etcd.cancel_switchover(etcd.get("switchover"))
            print("Pending switchover request cancelled")
        elif args.action == "switchover":
            if args.target not in config["nodes"] or args.target == config["node"]:
                raise ValueError("Choose a different configured node")
            etcd.request_switchover(config["node"], args.target)
            print("Switchover requested; monitor both services and the status API")
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
