#!/bin/bash

export ETCDCTL_ENDPOINTS="http://<ETCD_NODE_1>:2379,http://<ETCD_NODE_2>:237"

function log_func {
  # Get the current date
  current_date=$(date +%Y-%m-%d)

  # Create a log file in /var/log/saturn/ named saturn-log-current-date
  logfile="/var/log/saturn/saturn-log-$current_date"

  # Retention policy: delete logs older than 7 days
  find /var/log/saturn/ -type f -mtime +7 -delete

  if [ ! -f "$logfile" ]; then
    touch "$logfile"
    echo "Created log file $logfile" >> "$logfile"
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

function check_health {
  # Get the status of all nodes in the etcd cluster
  cluster_status=$(etcdctl endpoint health)

  # Check if all nodes are healthy
  if [[ $cluster_status =~ "unhealthy" ]]; then
    echo "Some nodes are unhealthy." >> "$logfile"
  else
    echo "All nodes are healthy." >> "$logfile"
  fi
}

while true; do
  log_func
  isLeader
  sleep 1
  # check_health
done
