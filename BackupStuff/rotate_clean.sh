#!/bin/bash

PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

# Move archives to monthly.

# prod_system_1
find /mnt/backupsrv/weekly/prod_system_1/ -type f -mtime -1 -exec mv -f {} /mnt/backupsrv/monthly/prod_system_1/ \;
# prod_system_2
find /mnt/backupsrv/weekly/prod_system_2/ -type f -mtime -1 -exec mv -f {} /mnt/backupsrv/monthly/prod_system_2/ \;

sleep 15

# Move archives to yearly

LAST_SAT=`cal | awk '{print $7}' | awk '{print $NF}' | grep -v '^$' | tail -n 1 | head -1`

DATE=`echo $(date +%e)`

if [ $DATE -eq $LAST_SAT ]
   then
    # prod_system_1
    find /mnt/backupsrv/monthly/prod_system_1/ -type f -mtime -1 -exec mv -f {} /mnt/backupsrv/yearly/prod_system_1/ \;
    # prod_system_2
    find /mnt/backupsrv/monthly/prod_system_2/ -type f -mtime -1 -exec mv -f {} /mnt/backupsrv/yearly/prod_system_2/ \;
fi

# Cleaning script for monthly backup. Remaining a quartal.

# prod_system_1
find /mnt/backupsrv/monthly/prod_system_1/ -type f -mtime +92 -delete
# prod_system_2
find /mnt/backupsrv/monthly/prod_system_2/ -type f -mtime +92 -delete

exit 0
