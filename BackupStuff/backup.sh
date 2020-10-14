#! /bin/bash

# Synchronizing files and directories that need to be backed up на /mnt/backup_storage
rsync -avzhe --omit-dir-times --no-perms \
	/var/lib/data1 \
	/opt/prog/data2 \
	/home/srvadm/passwords.kdbx \
	/mnt/backup_storage

# Copying the local file to the remote server
scp -P 22422 /home/srvadm/passwords.kdbx remoteuser@185.127.224.232:/home/remoteuser
# Copying files from the remote server to the local host
scp -P 22422 remoteuser@158.127.234.212:/home/remoteuser/\{pass1.kdbx,pass2.kdbx,pass3.kdbx\} /home/srvadm

