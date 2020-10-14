#!/bin/bash

# Get Data from DMZ

# Moving MailServer backup archive.
scp 'root@172.20.0.12:/mnt/lcstore/dump/mail*' /mnt/backupsrv/weekly/mail/

# Remove MailServer backup archive.
ssh root@172.20.0.12 'rm /mnt/lcstore/dump/mail*'
