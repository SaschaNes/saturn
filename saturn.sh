#!/bin/bash

# --------------------- changes here -------------------------
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
# ------------------------------------------------------------


# Tell the system what ETCD nodes are in the ETCD cluster
export ETCDCTL_ENDPOINTS="http://$node1:2379,http://$node2:2379,http://$node3:2379"

# Get current leader
old_leader=$(etcdctl member list | grep "isLeader=true" | awk -F: '{print $2}' | awk '{print $1}' | cut -c 6-)

function log_func {
  # Get the current date
  current_date=$(date +%Y-%m-%d)

  # Create a log file in /var/log/saturn/ named saturn-log-current-date
  logfile="/var/log/saturn/saturn-log-$current_date"
  rsynclog="/var/log/saturn/saturn_rsync-log-$current_date"

  # Retention policy: delete logs older than 7 days
  find /var/log/saturn/ -type f -mtime +7 -delete

  # check if logfiles are already created, if not create them
  if [ ! -f "$logfile" ]; then
    touch "$logfile"
    echo "Created log file $logfile" >> "$logfile"
  else
    return
  fi

  if [ ! -f "$rsynclog" ]; then
    touch "$rsynclog"
    echo "Created log file $rsynclog" >> "$rsynclog"
  else
    return
  fi
}

function isLeader {
  # Get the leader key from etcd
  leader_hostname=$(etcdctl member list | grep "isLeader=true" | awk -F: '{print $2}' | awk '{print $1}' | cut -c 6-)

  # Get the hostname of the current server
  current_server=$(uname -n)

  # Check if the current server is the leader
  if [ "$leader_hostname" == "$current_server" ]; then
    echo "$(date): I am $current_server, i am the current leader." >> "$logfile"
    return 0
  else
    echo "$(date): I am $current_server, i am following: $leader_hostname." >> "$logfile"
    return 1
  fi
}

function cryptoCheck {
  # Set SHA1SUM of test files that should be
  sha1pdf="c8a468b702f6f72777ec9e466bf69a3e96e7dd2d"
  sha1docx="de66e5ded334112aeb55cd21ef19c2e4ec9c8365"
  sha1txt="da39a3ee5e6b4b0d3255bfef95601890afd80709"
  sha1xlsx="f5ca96c15df165e0ad8a715d2976bcbd72e9f6ef"

  # Calculate the sha1sum of test files in /etc/saturn
  curPdfSha1=$(sha1sum /etc/saturn/file.pdf | awk '{print $1}')
  curDocxSha1=$(sha1sum /etc/saturn/file.docx | awk '{print $1}')
  curTxtSha1=$(sha1sum /etc/saturn/file.txt | awk '{print $1}')
  curXlsxSha1=$(sha1sum /etc/saturn/file.xlsx | awk '{print $1}')

  # Check if files are not encrypted
  if [ "$sha1pdf" == "$curPdfSha1" ] && [ "$sha1docx" == "$curDocxSha1" ] && [ "$sha1txt" == "$curTxtSha1" ] && [ "$sha1xlsx" == "$curXlsxSha1" ]; then
    return 0
  else
    return 1
  fi
}

function leaderChanged {
  # get current leader
  curr_leader=$(etcdctl member list | grep "isLeader=true" | awk -F: '{print $2}' | awk '{print $1}' | cut -c 6-)

  # check if old_leader at start is a different one than now
  # check if new leader is current host
  # if yes: promote current host
  # if no : demoting current host
  # after, set current leader als old leader
  # if leader same: skip
  if [ $old_leader != $curr_leader ]; then
    isLeader
    if [ $? -eq 0 ]; then
      promote
      echo "$current_date: Promoting $curr_leader" >> $logfile
    else
      demote
      echo "$current_date: Demoting $old_leader" >> $logfile
    fi
    old_leader=$curr_leader
  else
    return
  fi
}

function syncStandby {
  # send changed files with rsync to standby nodes
  # check what node is currently leader and send to the other 2 nodes the data
  if [ $node1_hn = $(uname -n) ]; then
    rsync -av --perms --delete $dataDir root@$node2:$dataDir >> $rsynclog
    rsync -av --perms --delete $dataDir root@$node3:$dataDir >> $rsynclog
  fi
  if [ $node2_hn = $(uname -n) ]; then
    rsync -av --perms --delete $dataDir root@$node1:$dataDir >> $rsynclog
    rsync -av --perms --delete $dataDir root@$node3:$dataDir >> $rsynclog
  fi
  if [ $node3_hn = $(uname -n) ]; then
    rsync -av --perms --delete $dataDir root@$node1:$dataDir >> $rsynclog
    rsync -av --perms --delete $dataDir root@$node2:$dataDir >> $rsynclog
  fi
}

function enableVIP {
  # add virtual ip to using interface
  ip address add $virtual_ip/$subnet_cidr dev $interface
}

function disableVIP {
  # remove virtual ip from using interface
  ip address delete $virtual_ip/$subnet_cidr dev $interface
}

function promote {
  # set virtual ip on new leader and start samba
  enableVIP
  systemctl start smbd.service
}

function demote {
  # remove virtual ip of old leader and stop samba
  disableVIP
  systemctl stop smbd.service
}

while true; do
  leaderChanged
  log_func
  isLeader
  if [[ $? -eq 0 ]]; then
    cryptoCheck
    if [[ $? -eq 0 ]]; then
      syncStandby
    else
      demote
      systemctl stop etcd.service
    fi
  fi
  sleep 1
done
