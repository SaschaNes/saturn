#!/bin/bash

# --------------------- changes here -------------------------
# Set IP-Address of Nodes that should be in the saturn cluster
node1="<NODE_1_IPv4>"
node2="<NODE_2_IPv4>"

virtual_ip="<VIRTUAL_IP>"

# Tell where data-dir of Samba share is
dataDir="<FULL_PATH_SAMBA_DIR>"
# ------------------------------------------------------------


# Tell the system what ETCD nodes are in the ETCD cluster
export ETCDCTL_ENDPOINTS="http://$node1:2379,http://$node2:2379"

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
  curr_leader=$(etcdctl member list | grep "isLeader=true" | awk -F: '{print $2}' | awk '{print $1}' | cut -c 6-)

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
  rsync -av --perms --delete $dataDir root@$(etcdctl member list | grep "isLeader=false" | awk -F: '{print $2}' | awk '{print $1}' | cut -c 6-):$dataDir >> $rsynclog
}

function enableVIP {
  if [ ! -f "/etc/systemd/network/vip.*" ]; then
    cp /etc/saturn/virtual_ip/vip.* /etc/systemd/network/
    systemctl restart systemd-networkd
  else
    systemctl restart systemd-networkd
  fi
}

function disableVIP {
  ip link delete vip
}

function promote {
  enableVIP
  systemctl start smbd.service
}

function demote {
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
    fi
  fi
  sleep 1
done
