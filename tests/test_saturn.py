import http.server
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import backup_saturn
import saturn


CONFIG = {
    "node": "node1",
    "nodes": {"node1": "192.0.2.1", "node2": "192.0.2.2", "node3": "192.0.2.3"},
    "endpoints": ["http://127.0.0.1:2379"],
    "vip": "192.0.2.10/24", "interface": "eth0", "data_dir": "/data",
    "key_prefix": "/saturn/test", "lease_ttl": 30, "poll_interval": 3, "sync_interval": 30,
    "api_listen": "127.0.0.1", "api_port": 0, "require_mount": True,
}


class ConfigurationTests(unittest.TestCase):
    def test_invalid_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            config = dict(CONFIG, data_dir="/data/../secret")
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                saturn.load_config(path)


class GatewayTests(unittest.TestCase):
    def test_etcd_v3_transaction_is_sent_as_json(self):
        calls = []

        class Gateway(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append((self.path, body))
                payload = json.dumps({"succeeded": True}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = dict(CONFIG, endpoints=["http://127.0.0.1:" + str(server.server_port)])
            etcd = saturn.Etcd(config)
            self.assertTrue(etcd.txn([etcd.compare_value("owner", None)], [etcd.put("owner", "node1:token", 123)]))
            self.assertEqual(calls[0][0], "/v3/kv/txn")
            self.assertEqual(calls[0][1]["compare"][0]["version"], "0")
            self.assertEqual(calls[0][1]["success"][0]["request_put"]["lease"], "123")
        finally:
            server.shutdown()
            server.server_close()


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.etcd = saturn.Etcd(CONFIG)
        self.etcd.ttl = 30

    def test_acquire_requires_approval_and_empty_owner_atomically(self):
        approval = json.dumps({"node": "node1", "previous": "node2", "id": "once"})
        self.etcd.grant = MagicMock(return_value=123)
        self.etcd.txn = MagicMock(return_value=True)
        token, lease = self.etcd.acquire("node1", approval)
        self.assertTrue(token.startswith("node1:"))
        self.assertEqual(lease, 123)
        compares, writes = self.etcd.txn.call_args.args
        self.assertEqual([part["target"] for part in compares], ["VERSION", "VALUE", "VALUE"])
        self.assertEqual([list(part) for part in writes], [["request_put"], ["request_put"], ["request_delete_range"]])
        self.assertEqual(writes[0]["request_put"]["lease"], "123")

    def test_wrong_candidate_does_not_grant_lease(self):
        self.etcd.grant = MagicMock()
        approval = json.dumps({"node": "node2", "previous": None, "id": "once"})
        self.assertIsNone(self.etcd.acquire("node1", approval))
        self.etcd.grant.assert_not_called()

    def test_existing_owner_prevents_manual_approval(self):
        self.etcd.get = MagicMock(return_value="node2")
        self.etcd.txn = MagicMock(return_value=False)
        with self.assertRaises(saturn.EtcdError):
            self.etcd.approve("node1")
        compares, writes = self.etcd.txn.call_args.args
        self.assertEqual(compares[0], self.etcd.compare_value("owner", None))
        self.assertEqual(compares[1], self.etcd.compare_value("approval", None))
        self.assertEqual(writes[0]["request_put"]["key"], self.etcd.key("approval"))

    def test_switchover_requires_current_owner_and_atomic_handoff(self):
        self.etcd.get = MagicMock(return_value="node1:token")
        self.etcd.txn = MagicMock(return_value=True)
        self.etcd.request_switchover("node1", "node2")
        compares, writes = self.etcd.txn.call_args.args
        self.assertEqual(compares[0], self.etcd.compare_value("owner", "node1:token"))
        self.assertEqual(compares[1], self.etcd.compare_value("switchover", None))
        self.assertEqual(writes[0]["request_put"]["key"], self.etcd.key("switchover"))
        self.etcd.revoke = MagicMock()
        self.etcd.handoff("node1:token", "request", "node2", 123)
        compares, writes = self.etcd.txn.call_args.args
        self.assertEqual(compares[0], self.etcd.compare_value("owner", "node1:token"))
        self.assertEqual(compares[1], self.etcd.compare_value("switchover", "request"))
        self.assertEqual([list(part) for part in writes],
                         [["request_delete_range"], ["request_delete_range"], ["request_put"]])
        self.assertEqual(json.loads(saturn.decoded(writes[2]["request_put"]["value"]))["node"], "node2")
        self.etcd.revoke.assert_called_once_with(123)

    def test_failed_handoff_does_not_approve_target(self):
        self.etcd.txn = MagicMock(return_value=False)
        self.etcd.revoke = MagicMock()
        with self.assertRaises(saturn.EtcdError):
            self.etcd.handoff("node1:token", "request", "node2", 123)
        self.etcd.revoke.assert_not_called()

    def test_renew_uses_compare_and_swap(self):
        self.etcd.grant = MagicMock(return_value=456)
        self.etcd.revoke = MagicMock()
        self.etcd.txn = MagicMock(return_value=True)
        self.assertEqual(self.etcd.renew("node1:token", 123), 456)
        compares, writes = self.etcd.txn.call_args.args
        self.assertEqual(compares, [self.etcd.compare_value("owner", "node1:token")])
        self.assertEqual(writes, [self.etcd.put("owner", "node1:token", 456)])
        self.etcd.revoke.assert_called_once_with(123)

    def test_lost_ownership_is_not_released(self):
        self.etcd.txn = MagicMock(return_value=False)
        self.etcd.revoke = MagicMock()
        self.etcd.release("node1:token", 123)
        self.etcd.revoke.assert_not_called()

    def test_failed_renew_demotes_and_never_promotes_without_approval(self):
        etcd = MagicMock()
        etcd.renew.side_effect = saturn.EtcdError("lost quorum")
        etcd.get.return_value = None
        coordinator = saturn.Coordinator(CONFIG, etcd)
        coordinator.ownership = ("node1:token", 123)
        def demote():
            if coordinator.demote.call_count == 2:
                coordinator.running = False
        coordinator.demote = MagicMock(side_effect=demote)
        coordinator.serve_status = MagicMock(return_value=MagicMock())
        with patch.object(saturn.time, "sleep"), self.assertLogs("saturn", level="ERROR"):
            coordinator.run()
        self.assertEqual(coordinator.demote.call_count, 3)  # startup, failure, shutdown
        etcd.release.assert_called_once_with("node1:token", 123)
        etcd.acquire.assert_not_called()

    def test_missing_mount_blocks_promotion(self):
        coordinator = saturn.Coordinator(dict(CONFIG, require_mount=True), MagicMock())
        with patch.object(saturn.Path, "is_dir", return_value=True), patch.object(saturn.os.path, "ismount", return_value=False):
            with patch.object(saturn, "command") as command:
                with self.assertRaises(RuntimeError):
                    coordinator.promote()
                command.assert_not_called()

    def test_status_endpoints_fail_closed(self):
        coordinator = saturn.Coordinator(CONFIG, MagicMock())
        server = coordinator.serve_status()
        url = "http://127.0.0.1:" + str(server.server_port)
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(url + "/primary")
            self.assertEqual(error.exception.code, 503)
            error.exception.close()
            coordinator.set_role("primary")
            coordinator.note_dcs()
            with urllib.request.urlopen(url + "/primary") as response:
                self.assertEqual(json.load(response)["role"], "primary")
            coordinator.note_dcs_lost()
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(url + "/health")
            self.assertEqual(error.exception.code, 503)
            error.exception.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_planned_handoff_quiesces_before_final_sync(self):
        coordinator = saturn.Coordinator(CONFIG, MagicMock())
        coordinator.ownership = ("node1:token", 123)
        coordinator.demote = MagicMock()
        coordinator.check_data = MagicMock()
        coordinator.final_sync = MagicMock()
        request = json.dumps({"owner": "node1:token", "target": "node2"})
        coordinator.start_handoff(request)
        coordinator.handoff_thread.join(timeout=2)
        coordinator.demote.assert_called_once()
        coordinator.final_sync.assert_called_once()
        self.assertEqual(coordinator.status()["role"], "switchover")

    def test_target_with_active_samba_never_receives_final_sync(self):
        coordinator = saturn.Coordinator(CONFIG, MagicMock())
        coordinator.handoff_target = "node2"
        with patch.object(saturn.subprocess, "run", side_effect=[MagicMock(returncode=0), MagicMock(returncode=0)]):
            with patch.object(coordinator, "replicate") as replicate, self.assertLogs("saturn", level="ERROR"):
                coordinator.final_sync()
                replicate.assert_not_called()
        self.assertFalse(coordinator.handoff_result)

    def test_planned_handoff_commits_only_after_final_sync(self):
        etcd = MagicMock()
        request = json.dumps({"owner": "node1:token", "target": "node2"})
        etcd.get.return_value = request
        etcd.renew.return_value = 456
        coordinator = saturn.Coordinator(CONFIG, etcd)
        coordinator.ownership = ("node1:token", 123)
        coordinator.serve_status = MagicMock(return_value=MagicMock())
        coordinator.demote = MagicMock()
        coordinator.vip_present = MagicMock(return_value=True)
        coordinator.check_data = MagicMock()
        coordinator.final_sync = MagicMock(side_effect=lambda: setattr(coordinator, "handoff_result", True))
        etcd.handoff.side_effect = lambda *_args: setattr(coordinator, "running", False)
        original_sleep = time.sleep
        with patch.object(saturn, "command"), patch.object(saturn.time, "sleep", side_effect=lambda _: original_sleep(0.01)):
            coordinator.run()
        self.assertGreaterEqual(coordinator.demote.call_count, 3)
        etcd.handoff.assert_called_once_with("node1:token", request, "node2", 456)
        etcd.release.assert_not_called()
        self.assertEqual(coordinator.status()["role"], "standby")

    def test_failed_final_sync_cannot_approve_target(self):
        etcd = MagicMock()
        request = json.dumps({"owner": "node1:token", "target": "node2"})
        etcd.get.return_value = request
        etcd.renew.return_value = 456
        coordinator = saturn.Coordinator(CONFIG, etcd)
        coordinator.ownership = ("node1:token", 123)
        coordinator.serve_status = MagicMock(return_value=MagicMock())
        coordinator.demote = MagicMock()
        coordinator.vip_present = MagicMock(return_value=True)
        coordinator.check_data = MagicMock()
        coordinator.final_sync = MagicMock(side_effect=lambda: setattr(coordinator, "handoff_result", False))
        etcd.release.side_effect = lambda *_args: setattr(coordinator, "running", False)
        original_sleep = time.sleep
        with patch.object(saturn, "command"), patch.object(saturn.time, "sleep", side_effect=lambda _: original_sleep(0.01)):
            with self.assertLogs("saturn", level="ERROR"):
                coordinator.run()
        etcd.handoff.assert_not_called()
        etcd.cancel_switchover.assert_called_once_with(request)
        etcd.release.assert_called_once_with("node1:token", 456)


class BackupTests(unittest.TestCase):
    def test_failed_backup_preserves_existing_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "daily" / "20260101T000000Z"
            old.mkdir(parents=True)
            (old / "important").write_text("data")
            etcd = MagicMock()
            etcd.get.return_value = "node1:token"
            with patch.object(backup_saturn, "run_rsync", side_effect=RuntimeError("failed")):
                with self.assertRaises(RuntimeError):
                    backup_saturn.backup(CONFIG, etcd, root, "daily", 1)
            self.assertEqual((old / "important").read_text(), "data")
            self.assertEqual(len(backup_saturn.snapshots(root, "daily")), 1)

    def test_restore_rejects_live_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            etcd = MagicMock()
            etcd.get.return_value = "node1:token"
            with patch.object(backup_saturn, "run_rsync") as run:
                with self.assertRaises(RuntimeError):
                    backup_saturn.restore(CONFIG, etcd, Path(directory), "daily", "20260101T000000Z", "node2")
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
