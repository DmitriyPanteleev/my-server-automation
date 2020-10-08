#!/bin/bash

# Cleaning script for weekly backup

# Prod_1
find /mnt/backupsrv/weekly/Prod_1/ -type f -mtime +7 -exec mv -f {} /mnt/backupsrv/temp \;

# Prod_2
find /mnt/backupsrv/weekly/Prod_2/ -type f -mtime +7 -exec mv -f {} /mnt/backupsrv/temp \;
