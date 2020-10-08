#! /bin/bash

# Сихронизируем файлы и директории которые нужно бэкапить на /mnt/backup_storage
rsync -avzhe --omit-dir-times --no-perms \
	/var/lib/data1 \
	/opt/prog/data2 \
	/home/srvadm/passwords.kdbx \
	/mnt/backup_storage

# Копируем локальный файл на удаленный сервер
scp -P 22422 /home/srvadm/passwords.kdbx remoteuser@185.127.224.232:/home/remoteuser
# Копируем файлы с удаленного сервера на локальный хост
scp -P 22422 remoteuser@158.127.234.212:/home/remoteuser/\{pass1.kdbx,pass2.kdbx,pass3.kdbx\} /home/srvadm

