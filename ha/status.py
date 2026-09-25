#!/usr/bin/env python3
"""Read-only Pacemaker status validation for the Saturn HA profile."""

import argparse
import http.server
import json
import socket
import subprocess
import xml.etree.ElementTree as ET


class UnsafeCluster(RuntimeError):
    pass


def check(xml, expected_nodes):
    root = ET.fromstring(xml)
    summary = root.find("summary")
    if summary is None:
        raise UnsafeCluster("Missing Pacemaker summary")
    dc = summary.find("current_dc")
    options = summary.find("cluster_options")
    if dc is None or dc.get("with_quorum") != "true":
        raise UnsafeCluster("Cluster has no quorum")
    if options is None or options.get("stonith-enabled") != "true" or options.get("no-quorum-policy") != "stop":
        raise UnsafeCluster("STONITH or no-quorum-policy is unsafe")
    if options.get("maintenance-mode") != "false" or options.get("stop-all-resources") != "false":
        raise UnsafeCluster("Cluster is in maintenance or resources are stopped")
    nodes = root.find("nodes")
    if nodes is None or {node.get("name") for node in nodes.findall("node")} != set(expected_nodes):
        raise UnsafeCluster("Node list does not match the HA configuration")
    online = {node.get("name") for node in nodes.findall("node") if node.get("online") == "true"}
    if len(online) < 2:
        raise UnsafeCluster("Fewer than two online cluster nodes")
    for node in nodes.findall("node"):
        if node.get("unclean") == "true":
            raise UnsafeCluster("Unclean node: " + str(node.get("name")))
    resources = root.find("resources")
    if resources is None:
        raise UnsafeCluster("Missing Pacemaker resources")
    promoted_drbd = [item for item in resources.iter("resource")
                     if item.get("id") == "p_drbd_saturn" and item.get("role") == "Promoted"]
    if (len(promoted_drbd) != 1 or promoted_drbd[0].get("active") != "true"
            or promoted_drbd[0].get("failed") == "true" or promoted_drbd[0].get("managed") == "false"):
        raise UnsafeCluster("Expected exactly one promoted DRBD resource")
    primary_nodes = promoted_drbd[0].findall("node")
    if len(primary_nodes) != 1:
        raise UnsafeCluster("DRBD promotion has no unique node")
    primary = primary_nodes[0].get("name")
    if primary not in online:
        raise UnsafeCluster("DRBD primary is not an online node")
    for resource_id in ("p_fs_saturn", "p_smb_saturn", "p_vip_saturn"):
        matches = [item for item in resources.iter("resource") if item.get("id") == resource_id]
        if (len(matches) != 1 or matches[0].get("active") != "true" or matches[0].get("failed") == "true"
                or matches[0].get("managed") == "false"):
            raise UnsafeCluster(resource_id + " is not healthy and active")
        running = matches[0].findall("node")
        if len(running) != 1 or running[0].get("name") != primary:
            raise UnsafeCluster(resource_id + " is not colocated with promoted DRBD")
    for node in expected_nodes:
        fencing = [item for item in resources.iter("resource") if item.get("id") == "fence_" + node]
        if len(fencing) != 1 or fencing[0].get("resource_agent") != "stonith:fence_ipmilan" or fencing[0].get("managed") == "false":
            raise UnsafeCluster("Missing or incorrect IPMI fencing resource for " + node)
    return {"primary": primary, "quorum": True, "stonith": True, "resources_colocated": True}


def active_primary(expected_nodes):
    result = subprocess.run(["crm_mon", "--output-as=xml"], capture_output=True, text=True,
                            timeout=10, check=True)
    return check(result.stdout, expected_nodes)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/status", "/health", "/primary", "/standby"):
            self.send_error(404)
            return
        try:
            report = active_primary(self.server.expected_nodes)
            report["healthy"] = True
        except (OSError, ValueError, ET.ParseError, subprocess.SubprocessError, UnsafeCluster) as exc:
            report = {"healthy": False, "error": str(exc)}
        report["node"] = self.server.node
        ok = (self.path == "/status" or
              (report["healthy"] and (self.path == "/health" or
               (self.path == "/primary" and report["primary"] == self.server.node) or
               (self.path == "/standby" and report["primary"] != self.server.node))))
        body = json.dumps(report).encode()
        self.send_response(200 if ok else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        pass


def serve(expected_nodes, node, port=8008):
    if node not in expected_nodes:
        raise ValueError("Local node is not listed in the HA configuration")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.expected_nodes = expected_nodes
    server.node = node
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", help="Private cluster JSON used for the HA plan")
    parser.add_argument("--serve", action="store_true", help="Serve local, read-only health endpoints")
    parser.add_argument("--node", default=socket.gethostname().split(".")[0])
    parser.add_argument("--port", type=int, default=8008)
    args = parser.parse_args()
    try:
        with open(args.config) as stream:
            config = json.load(stream)
        names = [node["name"] for node in config["nodes"]]
        if args.serve:
            with serve(names, args.node, args.port) as server:
                server.serve_forever()
        else:
            print(json.dumps(active_primary(names)))
    except (OSError, ValueError, KeyError, ET.ParseError, subprocess.SubprocessError, UnsafeCluster) as exc:
        parser.exit(1, "Cluster status cannot be trusted: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
