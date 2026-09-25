import json
import tempfile
import unittest
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
}


class ConfigurationTests(unittest.TestCase):
    def test_invalid_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            config = dict(CONFIG, data_dir="/data/../secret")
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                saturn.load_config(path)


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
