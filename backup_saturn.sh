virtual_ip="10.211.55.7"
dataDir="/data/"
backupDir="/tmp/data/"

RED='\033[0;31m'

if [ "$1" != "" ] && [ "$2" != "" ]; then
  echo "Setting option with $1"
  option=$1
  echo "Setting option with $2"
  option2=$2
else
  printf "${RED}No argument was given, run backup_saturn --help to get help."
  exit 3
fi

if [ $option = "--help" ]; then
  echo "This command is a tool, to backup samba directorys from a saturn cluster."
  echo "Usage: backup_saturn.sh [--ARGUMENT-1] [--ARGUMENT-2]"
  echo "For --ARGUMENT-1 you can use follwing options:"
  echo "--help   :      Show this help dialog."
  echo ""
  echo "--daily  :      Create a daily backup, create a folder is there is none,"
  echo "                delete backups that are older than 1 day."
  echo ""
  echo "--weekly :      Create a weekly backup, create a folder is there is none,"
  echo "                delete backups that are older than 7 days."
  echo ""
  echo "--monthly:      Create a monthly backup, create a folder is there is none,"
  echo "                delete backups that are older than 30 days."
  echo ""
  echo "For --ARGUMENT-2 you can use follwing options:"
  echo "--backup :      Tell the script to backup, use for argument-1 one of the"
  echo "                3 available times."
  echo ""
  echo "--restore:      Run a restore from daily, weekly or monthly backup."
fi

if [[ $option = "--backup" ]]; then
  if [ $option2 = "--daily" ]; then
    if [ ! -d "$backupDir/daily" ]; then
      mkdir "$backupDir/daily"
    fi
    rsync -av --ignore-times --perms --delete root@$virtual_ip:$dataDir $backupDir/daily
    find $backupDir/daily/ -type f -mtime +1 -delete
  elif [ $option2 = "--weekly" ]; then
    if [ ! -d "$backupDir/weekly" ]; then
      mkdir "$backupDir/weekly"
    fi
    rsync -av --ignore-times --perms --delete root@$virtual_ip:$dataDir $backupDir/weekly
    find $backupDir/weekly/ -type f -mtime +7 -delete
  elif [ $option2 = "--monthly" ]; then
    if [ ! -d "$backupDir/monthly" ]; then
      mkdir "$backupDir/monthly"
    fi
    rsync -av --ignore-times --perms --delete root@$virtual_ip:$dataDir $backupDir/monthly
    find $backupDir/weekly/ -type f -mtime +30 -delete
  else
    printf "${RED}Given argument was not valid, see --help for more information."
  fi
fi

if [[ $option = "--restore" ]]; then
  if [ $option2 = "--daily" ]; then
    if [ ! -d "$backupDir/daily/" ]; then
      printf "${RED}No daily backup avaible!"
    else
      rsync -av --perms --delete $backupDir/daily/ root@$virtual_ip:$dataDir
    fi
  elif [ $option2 = "--weekly" ]; then
    if [ ! -d "$backupDir/weekly/" ]; then
      printf "${RED}No weekly backup avaible!"
    else
      rsync -av --perms --delete $backupDir/weekly/share root@$virtual_ip:$dataDir
    fi
  elif [ $option2 = "--monthly" ]; then
    if [ ! -d "$backupDir/monthly/" ]; then
      printf "${RED}No monthly backup avaible!"
    else
      rsync -av --perms --delete $backupDir/monthly/$dataDir root@$virtual_ip:$dataDir
    fi
  else
    printf "${RED}Given argument was not valid, see --help for more information."
  fi
fi
