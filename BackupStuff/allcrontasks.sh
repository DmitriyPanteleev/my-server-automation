#! /bin/bash

# Определим цвета вывода
RED='\e[1;31m'
GREEN='\e[1;32m'
NC='\e[0m'

# Определим нашу функцию вывода списка всех задач cron у всех пользователей
for user in $(cut -d':' -f1 /etc/passwd); do
    usercrontab=$(crontab -l -u ${user} 2>/dev/null)
    if [ -n "${usercrontab}" ]; then
         echo -e "${RED}====== Start crontab for user ${NC}${GREEN}${user}${NC} ${RED}======${NC}"
         crontab -l -u ${user} | sed '/ *#/d; /^ *$/d'
         echo -e "${RED}====== End crontab for user ${NC}${GREEN}${user}${NC} ${RED}========${NC}\n"
    fi
done

