#!/bin/bash
PATH=/etc:/bin:/sbin:/usr/bin:/usr/sbin:/usr/local/bin:/usr/local/sbin
 
source="/data"
destination_root="/backup"
fdate=$(date +%Y-%m-%d)
 
# Clean old archives
find ${destination_root}/archive -type f -name "*.tar.gz" -ctime +370 -exec rm -R {} \; 2>&1
 
# Daily
rsync -a --partial --inplace --append --delete-after ${source}/ ${destination_root}/daily/
 
# Weekly
if [[ $(date +%u) -eq 0 ]]
then
  rsync -a --partial --inplace --append --delete-after ${source}/ ${destination_root}/weekly/
fi
 
# Archive
count_last_archives=$(find ${destination_root}/archive/ -name "*.tar.gz" -mtime -30 | wc -l)
if [[ $count_last_archives -eq 0 ]]
then
  cd ${source}
  tar zcf ${destination_root}/archive/samba_${fdate}.tar.gz ./*
fi
