# Saturn
High Availability Service for Samba

Tested and supported for:
- Ubuntu Server 22.04.1 LTS
- Debian 11

### What is Saturn?
Saturn is a service that provides high availability for Samba, a popular open-source file sharing service.
It leverages the etcd distributed key-value store to get a leader in the cluster.

### Prerequirements
- etcd (v2 API) installed and configured
- rsync installed
- 3 nodes that are reachable to each other

### How to install Saturn?

#### Installing and configuring etcd

If you are running Ubuntu or Debian, you should be able to install etcd over apt. You will need a version that is not higher than 3.4, because the script uses ETCD v2 API. And since ETCD 3.4, the v2 API is not available anymore.
```
sudo apt install etcd
```

After that you will need to configure etcd.
Navigate to ```/etc/etcd/``` and edit the ```etcd.conf``` file.
Copy the following config and edit it for the host you are working on currently.
Keep in mind that the etcd node name should be the same as the hostname on the node itself.
```
_ETCD_NAME="<NODE-ETCD_NAME>"
_ETCD_DATA_DIR="/var/lib/etcd/saturn"
_ETCD_LISTEN_PEER_URLS="http://<NODE-IPV4>:2380"
_ETCD_LISTEN_CLIENT_URLS="http://<NODE-IPV4>:2379"
_ETCD_INITIAL_ADVERTISE_PEER_URLS="http://<NODE-IPV4>:2380"
_ETCD_ADVERTISE_CLIENT_URLS="http://<NODE-IPV4>:2379"
_ETCD_INITIAL_CLUSTER_TOKEN="etcd-saturn"
_ETCD_INITIAL_CLUSTER_STATE="new"
_ETCD_INITIAL_CLUSTER="<NODE-1-ETCD_NAME>=http://<Node-1-IPv4>:2380,<NODE-2-ETCD_NAME>=http://<Node-2-IPv4>:2380,<NODE-3-ETCD_NAME>=http://<Node-3-IPv4>:2380"
_ETCD_HEARTBEAT_INTERVAL=250
_ETCD_ELECTION_TIMEOUT=1250
_ETCD_ENABLE_V2=true
```

Edit this config on all 3 nodes that will be in the cluster.

Now you will need to edit the service file for etcd, this is located in ```/lib/systemd/system/etcd.service```

Change the service file to the following block:
```
[Unit]
Description=etcd service
After=network.target
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
EnvironmentFile=-/etc/etcd/etcd.conf

ExecStart=/usr/bin/etcd \
 --name=${_ETCD_NAME} \
 --data-dir=${_ETCD_DATA_DIR} \
 --initial-advertise-peer-urls=${_ETCD_INITIAL_ADVERTISE_PEER_URLS} \
 --listen-peer-urls=${_ETCD_LISTEN_PEER_URLS} \
 --listen-client-urls=${_ETCD_LISTEN_CLIENT_URLS} \
 --advertise-client-urls=${_ETCD_ADVERTISE_CLIENT_URLS} \
 --initial-cluster-token=${_ETCD_INITIAL_CLUSTER_TOKEN} \
 --initial-cluster=${_ETCD_INITIAL_CLUSTER} \
 --initial-cluster-state=${_ETCD_INITIAL_CLUSTER_STATE} \
 --heartbeat-interval=${_ETCD_HEARTBEAT_INTERVAL} \
 --election-timeout=${_ETCD_ELECTION_TIMEOUT} \
 --enable-v2=${_ETCD_ENABLE_V2}

Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Now run ```systemctl daemon-reload``` and ```systemctl enable etcd.service```.
At last, restart etcd with ```systemctl restart etcd.service``` on all 3 nodes.

To check if all nodes are working run these commands:

Change the placeholder like $node1 to the right IPs
```
export ETCDCTL_ENDPOINTS="http://$node1:2379,http://$node2:2379,http://$node3:2379"
```
And then run
```
etcdctl member list
```
At the end you should have 3 nodes that are healthy and one of them should be a leader.


#### Installing and configuring Saturn
Before you can install and run saturn, you will need to install and configure samba. For more information on how to install samba see [here](https://ubuntu.com/tutorials/install-and-configure-samba).

First install the prerequirements with apt
```
sudo apt install git rsync
```

Now clone this repository
```
git clone https://github.com/SaschaNes/saturn
```

Navigate into the cloned folder and copy the folders to their right places
```
cd saturn
cp -r etc/saturn/ /etc/saturn
cp -r saturn.sh /usr/bin/
cp -r lib/systemd/system/saturn.service /lib/systemd/system/saturn.service
```

Open the runable with nano/vim
```
nano /usr/bin/saturn.sh
```

Change the follwoing lines to the details with your cluster
```
# Set IP-Address of Nodes that should be in the saturn cluster
node1="<NODE_1_IPv4>"
node1_hn="<NODE_1_HOSTNAME>"

node2="<NODE_2_IPv4>"
node2_hn="<NODE_2_HOSTNAME>"

node3="<NODE_3_IPv4>"
node3_hn="<NODE_2_HOSTNAME>"

# Set details of virtual ip /IP itself, subnet in cidr notation,
# and the interface that should be used
virtual_ip="<VIRTUAL_IP>"
subnet_cidr="<SUBNET_FROM_VIP>"
interface="<INTERFACE_FOR_VIP>"

# Tell where data-dir of Samba share is
dataDir="<FULL_PATH_SAMBA_DIR>"
```

Run ```systemctl daemon-reload``` and ```systemctl enable saturn.service```. Now start saturn with
```
systemctl start saturn.service
```
