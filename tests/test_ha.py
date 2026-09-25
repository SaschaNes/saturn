import copy
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from ha import preflight, render, status


CONFIG = {
    "nodes": [
        {"name": f"node{i}", "cluster_ip": f"10.10.0.{i}",
         "replication_ip": f"10.20.0.{i}", "bmc_ip": f"10.30.0.{i}", "bmc_user": "fencer"}
        for i in range(1, 4)
    ],
    "drbd": {"resource": "saturn_data", "device": "/dev/drbd1000",
             "backing_device": "/dev/mapper/saturn_data", "port": 7788},
    "filesystem": {"mount": "/srv/saturn/data", "fstype": "ext4"},
    "vip": {"address": "10.10.0.100/24", "interface": "ens18"},
    "fencing": {"agent": "fence_ipmilan", "password_script_dir": "/usr/local/libexec/saturn/fence"},
}

XML = """<pacemaker-result>
  <summary><current_dc with_quorum="true"/>
    <cluster_options stonith-enabled="true" no-quorum-policy="stop"
      maintenance-mode="false" stop-all-resources="false"/></summary>
  <nodes><node name="node1" online="true"/><node name="node2" online="true"/>
    <node name="node3" online="true"/></nodes>
  <resources>
    <clone><resource id="p_drbd_saturn" role="Promoted" active="true"><node name="node1"/></resource>
      <resource id="p_drbd_saturn" role="Unpromoted" active="true"><node name="node2"/></resource></clone>
    <group><resource id="p_fs_saturn" active="true"><node name="node1"/></resource>
      <resource id="p_smb_saturn" active="true"><node name="node1"/></resource>
      <resource id="p_vip_saturn" active="true"><node name="node1"/></resource></group>
    <resource id="fence_node1" resource_agent="stonith:fence_ipmilan"/>
    <resource id="fence_node2" resource_agent="stonith:fence_ipmilan"/>
    <resource id="fence_node3" resource_agent="stonith:fence_ipmilan"/>
  </resources>
  <status code="0" message="OK"/>
</pacemaker-result>"""


class RenderTests(unittest.TestCase):
    def test_generates_three_node_quorum_and_fencing_before_services(self):
        config = render.validate(copy.deepcopy(CONFIG))
        drbd = render.render_drbd(config)
        self.assertIn("auto-promote no;", drbd)
        self.assertIn("quorum majority;", drbd)
        self.assertIn("on-no-quorum suspend-io;", drbd)
        self.assertIn("protocol C;", drbd)
        self.assertEqual(drbd.count("node-id"), 3)
        plan = render.render_plan(config)
        self.assertLess(plan.index("pcs stonith create fence_node1"), plan.index("resource create p_drbd_saturn"))
        self.assertIn("no-quorum-policy=stop", plan)
        self.assertIn("with-rsc-role=Promoted", plan)
        self.assertIn("password_script=", plan)
        self.assertNotIn("stonith-enabled=false", plan.split("## 3.")[1])
        self.assertNotIn("mkfs.ext4", plan)
        self.assertEqual(len(render.render_secrets(config)), 3)

    def test_rejects_example_addresses_and_duplicate_bmc(self):
        with self.assertRaises(ValueError):
            render.validate(json.loads(Path("ha/cluster.example.json").read_text()))
        bad = copy.deepcopy(CONFIG)
        bad["nodes"][1]["bmc_ip"] = bad["nodes"][0]["bmc_ip"]
        with self.assertRaises(ValueError):
            render.validate(bad)
        bad = copy.deepcopy(CONFIG)
        bad["nodes"][0]["cluster_ip"] = "127.0.0.1"
        with self.assertRaises(ValueError):
            render.validate(bad)

    def test_rejects_unsafe_paths_and_unverified_fence_agent(self):
        bad = copy.deepcopy(CONFIG)
        bad["drbd"]["backing_device"] = "/dev/../sda"
        with self.assertRaises(ValueError):
            render.validate(bad)
        bad = copy.deepcopy(CONFIG)
        bad["fencing"]["agent"] = "fence_dummy"
        with self.assertRaises(ValueError):
            render.validate(bad)

    def test_redfish_requires_specific_system_uri_and_verified_tls(self):
        config = copy.deepcopy(CONFIG)
        config["fencing"]["agent"] = "fence_redfish"
        with self.assertRaises(ValueError):
            render.validate(config)
        for number, node in enumerate(config["nodes"], start=1):
            node["systems_uri"] = f"/redfish/v1/Systems/System{number}"
        render.validate(config)
        plan = render.render_plan(config)
        self.assertIn("fence_redfish", plan)
        self.assertIn("systems_uri=/redfish/v1/Systems/System1 ssl_secure=1", plan)
        self.assertNotIn("ssl_insecure", plan)
        xml = XML.replace("stonith:fence_ipmilan", "stonith:fence_redfish")
        self.assertEqual(status.check(xml, ["node1", "node2", "node3"], "fence_redfish")["primary"], "node1")
        with self.assertRaises(status.UnsafeCluster):
            status.check(xml, ["node1", "node2", "node3"], "fence_ipmilan")

    def test_render_writes_only_new_directory_and_no_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "rendered"
            config_file = Path(directory) / "cluster.json"
            config_file.write_text(json.dumps(CONFIG))
            subprocess.run([sys.executable, "ha/render.py", str(config_file), str(output)], check=True,
                           capture_output=True, text=True)
            self.assertIn("quorum majority", (output / "saturn_data.res").read_text())
            self.assertIn("password_script=", (output / "PLAN.md").read_text())
            self.assertNotIn("password123", (output / "PLAN.md").read_text())
            self.assertIn("exec cat", (output / "fence-scripts" / "node1").read_text())
            self.assertEqual((output / "fence-scripts" / "node1").stat().st_mode & 0o777, 0o700)
            again = subprocess.run([sys.executable, "ha/render.py", str(config_file), str(output)],
                                   capture_output=True, text=True)
            self.assertNotEqual(again.returncode, 0)  # no overwrite of reviewed artifacts


class StatusTests(unittest.TestCase):
    def test_healthy_cluster_has_one_primary(self):
        report = status.check(XML, ["node1", "node2", "node3"])
        self.assertEqual(report["primary"], "node1")

    def test_rejects_missing_fence_or_quorum(self):
        for unsafe in (XML.replace('with_quorum="true"', 'with_quorum="false"'),
                       XML.replace('<status code="0"', '<status code="4"'),
                       XML.replace('stonith-enabled="true"', 'stonith-enabled="false"'),
                       XML.replace('name="node2" online="true"', 'name="node2" online="false"').replace(
                           'name="node3" online="true"', 'name="node3" online="false"'),
                       XML.replace('<resource id="fence_node3" resource_agent="stonith:fence_ipmilan"/>', ""),
                       XML.replace('id="fence_node2" resource_agent="stonith:fence_ipmilan"',
                                   'id="fence_node2" resource_agent="ocf:heartbeat:Dummy"')):
            with self.subTest(unsafe=unsafe[-50:]), self.assertRaises(status.UnsafeCluster):
                status.check(unsafe, ["node1", "node2", "node3"])

    def test_rejects_split_primary_or_misplaced_samba(self):
        for unsafe in (XML.replace('role="Unpromoted"', 'role="Promoted"'),
                       XML.replace('id="p_smb_saturn" active="true"><node name="node1"',
                                   'id="p_smb_saturn" active="true"><node name="node2"')):
            with self.subTest(unsafe=unsafe[-50:]), self.assertRaises(status.UnsafeCluster):
                status.check(unsafe, ["node1", "node2", "node3"])

    def test_read_only_api_never_claims_primary_without_cluster_health(self):
        server = status.serve(["node1", "node2", "node3"], "node1", 0)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:" + str(server.server_port)
        try:
            with patch.object(status, "active_primary", side_effect=status.UnsafeCluster("lost quorum")):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(url + "/primary")
                self.assertEqual(error.exception.code, 503)
                error.exception.close()
                with urllib.request.urlopen(url + "/status") as response:
                    self.assertFalse(json.load(response)["healthy"])
            with patch.object(status, "active_primary", return_value={"primary": "node1", "quorum": True}):
                with urllib.request.urlopen(url + "/primary") as response:
                    self.assertTrue(json.load(response)["healthy"])
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(url + "/standby")
                error.exception.close()
        finally:
            server.shutdown()
            server.server_close()


class PreflightTests(unittest.TestCase):
    def test_missing_local_safety_components_block_review(self):
        with tempfile.TemporaryDirectory() as directory:
            problems = preflight.check(CONFIG, root=Path(directory), which=lambda _name: None)
        self.assertTrue(any("pacemaker-managed" in problem for problem in problems))
        self.assertTrue(any("DRBD 9" in problem for problem in problems))
        self.assertTrue(any("fencing credential" in problem for problem in problems))

    def test_rejects_agent_without_password_script(self):
        def fake_run(_args, **_kwargs):
            return subprocess.CompletedProcess([], 0, stdout="  ip\n  username\n  lanplus\n  method\n")

        with tempfile.TemporaryDirectory() as directory:
            problems = preflight.check(CONFIG, root=Path(directory),
                                       which=lambda name: "/usr/bin/" + name if name in ("pcs", "fence_ipmilan") else None,
                                       run=fake_run)
        self.assertTrue(any("password_script" in problem for problem in problems))

    def test_existing_vip_blocks_pacemaker_migration(self):
        def fake_run(_args, **_kwargs):
            return subprocess.CompletedProcess([], 0, stdout=json.dumps([
                {"addr_info": [{"local": "10.10.0.100"}]}
            ]))

        with tempfile.TemporaryDirectory() as directory:
            problems = preflight.check(CONFIG, root=Path(directory),
                                       which=lambda name: "/usr/sbin/ip" if name == "ip" else None,
                                       run=fake_run)
        self.assertTrue(any("VIP is already assigned" in problem for problem in problems))

if __name__ == "__main__":
    unittest.main()
