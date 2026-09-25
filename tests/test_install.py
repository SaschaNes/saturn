"""Offline tests for the staged installer; no SSH, disk or CIB changes."""

import copy
import contextlib
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from ha import install
from test_ha import CONFIG, XML


class InstallerTests(unittest.TestCase):
    def test_wizard_builds_valid_three_node_config_without_passwords(self):
        answers = ["saturn"]
        for i in range(1, 4):
            answers += [f"node{i}", f"10.10.0.{i}", f"10.20.0.{i}", f"10.30.0.{i}", "fencer"]
        answers += ["saturn_data", "/dev/drbd1000", "/dev/mapper/saturn_data", "7788",
                    "/srv/saturn/data", "10.10.0.100/24", "ens18", "1"]
        with patch("builtins.input", side_effect=answers), contextlib.redirect_stdout(io.StringIO()):
            config = install.wizard()
        self.assertEqual(config, {"cluster_name": "saturn", **CONFIG})
        self.assertNotIn("password", json.dumps(config["nodes"]))

    def test_private_config_is_exclusive_and_not_world_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cluster.json"
            install.save_config(path, CONFIG)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            if os.geteuid() == 0:
                self.assertEqual(install.load_config(path), CONFIG)
            with self.assertRaises(FileExistsError):
                install.save_config(path, CONFIG)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(install.InstallationError):
                install.save_config(link, CONFIG)

    def test_password_is_sent_only_over_stdin_and_file_is_exclusive(self):
        calls = []

        def fake_remote(_node, args, **kwargs):
            calls.append((args, kwargs))

        with patch.object(install, "remote", side_effect=fake_remote):
            install.write_remote(CONFIG["nodes"][0], "/etc/saturn/fencing/node1.password", "secret\n", 0o600)
        args, kwargs = calls[0]
        self.assertEqual(kwargs["input_data"], "secret\n")
        self.assertNotIn("secret", str(args))
        self.assertIn("set -C", args[-1])
        self.assertIn("umask 077", args[-1])

    def test_mutual_ssh_checks_all_six_directions_with_strict_hosts(self):
        calls = []

        def fake_remote(node, args, **_kwargs):
            calls.append((node["name"], args))

        with patch.object(install, "remote", side_effect=fake_remote):
            with contextlib.redirect_stdout(io.StringIO()):
                install.check_ssh_mesh(CONFIG)
        self.assertEqual(len(calls), 6)
        self.assertTrue(all("StrictHostKeyChecking=yes" in args for _, args in calls))
        self.assertTrue(all("BatchMode=yes" in args for _, args in calls))
        self.assertTrue(all("ForwardAgent=no" in args for _, args in calls))

    def test_prepare_stops_before_any_write_when_ssh_check_fails(self):
        with patch.object(install, "inspect_nodes"), patch.object(
                install, "check_ssh_mesh", side_effect=install.InstallationError("untrusted host")), patch.object(
                install, "write_remote") as write, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(install.InstallationError):
                install.prepare(CONFIG)
        write.assert_not_called()

    def test_enabled_legacy_service_blocks_prepare_even_when_inactive(self):
        def fake_remote(node, args, **_kwargs):
            if args[:2] == ["python3", "-c"]:
                facts = {"name": node["name"], "os": "ubuntu", "version": "24.04",
                         "ips": [node["cluster_ip"], node["replication_ip"]],
                         "commands": {key: True for key in install.PACKAGES}, "drbd9": True,
                         "ocf": {agent: True for agent in ("linbit/drbd", "heartbeat/Filesystem",
                                                       "heartbeat/IPaddr2")},
                         "legacy_active": False, "samba_active": False, "legacy_enabled": True,
                         "samba_enabled": False, "drbd_enabled": False, "cluster_exists": False,
                         "marker_exists": False, "config_exists": False}
                return subprocess.CompletedProcess(args, 0, stdout=json.dumps(facts))
            if args[0] == "findmnt" or args[:2] == ["test", "-e"]:
                return subprocess.CompletedProcess(args, 1)
            return subprocess.CompletedProcess(args, 0)

        with patch.object(install, "local_node"), patch.object(install, "remote", side_effect=fake_remote):
            with self.assertRaisesRegex(install.InstallationError, "services are active or enabled"):
                install.inspect_nodes(CONFIG, fresh=True)

    def test_no_resource_cib_written_when_storage_gate_fails(self):
        with patch.object(install, "inspect_nodes"), patch.object(install, "preflight_all"), patch.object(
                install, "verify_deployment"), patch.object(
                install, "cluster_ready"), patch.object(install, "storage_ready", side_effect=install.InstallationError(
                "Outdated disk")), patch.object(install, "run") as run, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(install.InstallationError):
                install.resources(CONFIG)
        run.assert_not_called()

    def test_changed_remote_drbd_configuration_blocks_bootstrap(self):
        def fake_remote(_node, args, **_kwargs):
            if args[1].endswith(".res"):
                digest = "0" * 64
            elif args[1] == install.CLUSTER_CONFIG:
                digest = hashlib.sha256((json.dumps(CONFIG, indent=2) + "\n").encode()).hexdigest()
            else:
                digest = "0" * 64
            return subprocess.CompletedProcess(args, 0, stdout=digest + "  " + args[1] + "\n")

        with patch.object(install, "remote", side_effect=fake_remote):
            with self.assertRaisesRegex(install.InstallationError, "differs"):
                install.verify_deployment(CONFIG)

    def test_cluster_gate_rejects_missing_fencing_or_quorum(self):
        root = ET.fromstring(XML)
        resources = root.find("resources")
        for item in list(resources):
            if item.tag in ("clone", "group"):
                resources.remove(item)
        safe = ET.tostring(root, encoding="unicode")
        for unsafe in (safe.replace('with_quorum="true"', 'with_quorum="false"'),
                       safe.replace('stonith-enabled="true"', 'stonith-enabled="false"'),
                       safe.replace('id="fence_node3"', 'id="unexpected"')):
            result = subprocess.CompletedProcess([], 0, stdout=unsafe)
            with self.subTest(unsafe=unsafe[-40:]), patch.object(install, "run", return_value=result):
                with self.assertRaises(install.InstallationError):
                    install.cluster_ready(CONFIG)
        with patch.object(install, "run", return_value=subprocess.CompletedProcess([], 0, stdout=safe)):
            install.cluster_ready(CONFIG)

    def test_storage_gate_rejects_missing_peer_or_outdated_replica(self):
        state = [{"name": "saturn_data", "node-id": 0, "role": "Secondary", "suspended": False,
                  "devices": [{"volume": 0, "disk-state": "UpToDate", "quorum": True}],
                  "connections": [{"peer-node-id": i, "connection-state": "Connected", "peer_devices": [
                      {"volume": 0, "peer-disk-state": "UpToDate", "replication-state": "Established",
                       "out-of-sync": 0}]} for i in (1, 2)]}]

        def fake_remote(_node, args, **_kwargs):
            if args[0] == "findmnt":
                return subprocess.CompletedProcess(args, 1)
            index = CONFIG["nodes"].index(_node)
            state[0]["node-id"] = index
            for peer, peer_index in zip(state[0]["connections"], sorted({0, 1, 2} - {index})):
                peer["peer-node-id"] = peer_index
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(state))

        with patch.object(install, "remote", side_effect=fake_remote):
            install.storage_ready(CONFIG)
            state[0]["connections"][0]["peer_devices"][0]["peer-disk-state"] = "Outdated"
            with self.assertRaises(install.InstallationError):
                install.storage_ready(CONFIG)
            state[0]["connections"][0]["peer_devices"][0]["peer-disk-state"] = "UpToDate"
            state[0]["role"] = "Primary"
            with self.assertRaises(install.InstallationError):
                install.storage_ready(CONFIG)

    def test_bootstrap_creates_only_fencing_resources(self):
        commands = []

        def fake_run(args, **_kwargs):
            commands.append(args)
            return subprocess.CompletedProcess(args, 0, stdout=XML)

        with patch.object(install, "inspect_nodes"), patch.object(install, "preflight_all"), patch.object(
                install, "verify_deployment"), patch.object(
                install, "check_ssh_mesh"), patch.object(install, "remote", return_value=subprocess.CompletedProcess(
                [], 1)), patch.object(install, "confirm"), patch.object(install, "run", side_effect=fake_run):
            with contextlib.redirect_stdout(io.StringIO()):
                install.bootstrap(copy.deepcopy(CONFIG))
        joined = "\n".join(" ".join(args) for args in commands)
        self.assertIn("pcs cluster setup saturn", joined)
        self.assertIn("pcs stonith create fence_node1", joined)
        self.assertNotIn("p_fs_saturn", joined)
        self.assertNotIn("cib-push", joined)


if __name__ == "__main__":
    unittest.main()
