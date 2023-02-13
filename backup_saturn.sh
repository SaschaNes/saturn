virtual_ip="<VIRTUAL_IP>"
dataDir="<FULL_PATH_SAMBA_DIR>"
backupDir="<FULL_PATH_BACKUP_DIR>"

option=$1

if [ $option = "--daily" ]; then
  if [ ! -d "$backupDir/daily" ]; then
    mkdir "$backupDir/daily"
  fi
  rsync -av --perms --delete root@$virtual_ip:$dataDir $backupDir/daily
  find $backupDir/daily/ -type f -mtime +1 -delete
fi

if [ $option = "--weekly" ]; then
  if [ ! -d "$backupDir/weekly" ]; then
    mkdir "$backupDir/weekly"
  fi
  rsync -av --perms --delete root@$virtual_ip:$dataDir $backupDir/weekly
  find $backupDir/weekly/ -type f -mtime +7 -delete
fi

if [ $option = "--monthly" ]; then
  if [ ! -d "$backupDir/monthly" ]; then
    mkdir "$backupDir/monthly"
  fi
  rsync -av --perms --delete root@$virtual_ip:$dataDir $backupDir/monthly
  find $backupDir/weekly/ -type f -mtime +30 -delete
fi
