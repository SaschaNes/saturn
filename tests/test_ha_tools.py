"""Optional offline integration checks against installed pcs/Pacemaker tools.

No command in this test talks to a live CIB or a BMC.
"""

import pathlib
import copy
import shlex
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

from ha.render import render_plan
from test_ha import CONFIG


@unittest.skipUnless(all(shutil.which(tool) for tool in ("pcs", "cibadmin", "crm_verify")),
                     "Pacemaker/pcs tools are not installed")
class OfflineCibTests(unittest.TestCase):
    def test_rendered_plan_builds_valid_offline_cib(self):
        with tempfile.TemporaryDirectory() as directory:
            cib = pathlib.Path(directory) / "cib.xml"
            empty = subprocess.run(["cibadmin", "--empty"], check=True, capture_output=True, text=True)
            cib.write_text(empty.stdout)

            for line in render_plan(CONFIG).splitlines():
                if not line.startswith("pcs "):
                    continue
                parts = shlex.split(line)
                if parts[1:3] in (["cluster", "cib"], ["cluster", "cib-push"]):
                    continue
                # WSL has no DRBD device, Samba unit, network interface or BMC.
                # Production must keep --agent-validation enabled.
                parts = [part for part in parts if part != "--agent-validation"]
                if "systemd:smbd" in parts and not pathlib.Path("/usr/lib/systemd/system/smbd.service").exists():
                    # Minimal distro containers have no Samba unit (or running systemd).
                    # This bypass is offline-only; the real plan must validate the agent.
                    parts.append("--force")
                if parts[1] == "-f":
                    parts[2] = str(cib)
                else:
                    # Never let a test edit the live CIB, even for fencing properties.
                    parts[1:1] = ["-f", str(cib)]
                result = subprocess.run(parts, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, msg=f"{line}\n{result.stderr}")

            verified = subprocess.run(["crm_verify", "-x", str(cib), "-V"],
                                      capture_output=True, text=True, timeout=30)
            self.assertEqual(verified.returncode, 0, msg=verified.stderr)
            root = ET.parse(cib).getroot()
            self.assertEqual(len(root.findall(".//primitive[@class='stonith']")), 3)
            clones = root.findall(".//clone")
            self.assertEqual(len(clones), 1)
            self.assertEqual(clones[0].find("meta_attributes/nvpair[@name='promotable']").get("value"), "true")
            self.assertEqual([item.get("id") for item in root.findall(".//group/primitive")],
                             ["p_fs_saturn", "p_smb_saturn", "p_vip_saturn"])
            order = root.find(".//rsc_order")
            self.assertEqual((order.get("first"), order.get("first-action"), order.get("then")),
                             ("p_drbd_saturn-clone", "promote", "g_saturn"))
            colocation = root.find(".//rsc_colocation")
            self.assertEqual((colocation.get("rsc"), colocation.get("with-rsc"),
                              colocation.get("with-rsc-role"), colocation.get("score")),
                             ("g_saturn", "p_drbd_saturn-clone", "Promoted", "INFINITY"))
            for name in ("fence_node1", "fence_node2", "fence_node3"):
                primitive = root.find(f".//primitive[@id='{name}']")
                values = {entry.get("name"): entry.get("value") for entry in primitive.findall("instance_attributes/nvpair")}
                self.assertEqual(values["method"], "onoff")
                self.assertIn("password_script", values)

    @unittest.skipUnless(shutil.which("fence_redfish"), "Redfish agent is not installed")
    def test_redfish_fence_resource_uses_verified_tls(self):
        config = copy.deepcopy(CONFIG)
        config["fencing"]["agent"] = "fence_redfish"
        for number, node in enumerate(config["nodes"], start=1):
            node["systems_uri"] = f"/redfish/v1/Systems/System{number}"
        with tempfile.TemporaryDirectory() as directory:
            cib = pathlib.Path(directory) / "cib.xml"
            cib.write_text(subprocess.run(["cibadmin", "--empty"], check=True,
                                          capture_output=True, text=True).stdout)
            command = next(line for line in render_plan(config).splitlines()
                           if line.startswith("pcs stonith create fence_node1 "))
            parts = shlex.split(command)
            parts[1:1] = ["-f", str(cib)]
            parts.remove("--agent-validation")  # BMC and password script do not exist in the lab.
            result = subprocess.run(parts, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            resource = ET.parse(cib).getroot().find(".//primitive[@id='fence_node1']")
            self.assertEqual(resource.get("type"), "fence_redfish")
            values = {entry.get("name"): entry.get("value") for entry in resource.findall("instance_attributes/nvpair")}
            self.assertEqual(values["ssl_secure"], "1")
            self.assertEqual(values["systems_uri"], "/redfish/v1/Systems/System1")


if __name__ == "__main__":
    unittest.main()
