#!/bin/bash

# Control script

# Control free space
df -h | grep -vE '^Filesystem|tmpfs|udev' | awk '{ print $5 " " $1 }' | while read output;

do
  echo $output
  usep=$(echo $output | awk '{ print $1}' | cut -d'%' -f1  )
  partition=$(echo $output | awk '{ print $2 }' )
  if [ $usep -ge 80 ]; then
    /home/backupsrv/scripts/sendemail.sh "Alert!!! Lack of disk space!" "Running out of space \"$partition ($usep%)\" ";
  fi

done

# Control backup files
# Prod_System_1
foundfiles=$(find /mnt/backupsrv/weekly/Prod_System_1/ -type f -mtime -1)
if [ "$foundfiles" = "" ]
    then /home/backupsrv/scripts/sendemail.sh "Lost Backup archive of Prod_System_1!!!" "Lost Backup archive of Prod_System_1"
fi

# Prod_System_2
foundfiles=$(find /mnt/backupsrv/weekly/Prod_System_2/ -type f -mtime -1)
if [ "$foundfiles" = "" ]
    then /home/backupsrv/scripts/sendemail.sh "Lost archive of Prod_System_1!!!" "Lost archive of Prod_System_1"
fi

/home/backupsrv/scripts/sendemail.sh "Backup archives control was done" "Backup archives control was done";
