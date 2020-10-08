#!/bin/bash
# From field
FROM=backupadmin@corpmail.com
# To field
MAILTO=humanadmin@corpmail.com
# Theme of mail
NAME=$1
# Mail body
BODY=$2
# SMTP server
SMTPSERVER=10.250.250.250
# SMTP credentials
SMTPLOGIN=backupadmin
SMTPPASS=superpassword

# Sending email
/usr/bin/sendEmail -f $FROM -t $MAILTO -o message-charset=utf-8  -u $NAME -m $BODY -s $SMTPSERVER -o tls=no -xu $SMTPLOGIN -xp $SMTPPASS
