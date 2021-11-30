#!/bin/bash

# основные переменные
PROXY_ADDRESS="localhost:${HTTP_PORT}"
DEADPOOL_URL="https://fakeurl-heater-dead-symbols-pool-staging.s3.amazonaws.com"
FILE_GROUP_ROOT="/tmp/udf_root_group"
FILE_GROUP_PRICESNAPSHOT="/tmp/udf_pricesnapshot_group"
FILE_METRIX_PATH="/tmp/deadpool"

# цвета консоли
RED_COLOR="\033[0;31m"
NO_COLOR="\033[0m"
YELLOW_COLOR="\033[1;33m"
GREEN_COLOR="\033[0;32m"

# даты
year=$(date +"%Y")
month=$(date +'%m' | sed 's/0//')

# функции скрипта
log () {
    printf "${2}%s %s\n${NO_COLOR}" "$(date '+%Y-%m-%d %H:%M:%S')" "${1}"
}

add_metric () {
    echo "udf.deadpool.info=${1}" > ${FILE_METRIX_PATH}
}

check_work () {
    if [ $? != 0 ]; then
        log "$1" "${RED_COLOR}"
        log "Завершаю работу." "${RED_COLOR}"
        add_metric 1
        exit
    else
        log "$2" "${GREEN_COLOR}"
    fi
}

# start
log "Запускаю очистку по некрологу для ${PROXY_ADDRESS}" "${YELLOW_COLOR}"
add_metric 0

# проверка на то что --path-price-snapshot-cache есть в /info прокси
log "Делаем проверку на наличие флага --path-price-snapshot-cache" "${YELLOW_COLOR}"
if [ ! "$(curl -s "${PROXY_ADDRESS}"/info | jq -r '.Cmdline[]|select(. | contains("--path-price-snapshot-cache"))')" ]; then
    log "Флаг --path-price-snapshot-cache отсутствует." "${RED_COLOR}"
    log "Завершаю работу." "${RED_COLOR}"
    add_metric 1
    exit
else
    log "Флаг --path-price-snapshot-cache присутствует." "${GREEN_COLOR}"
fi

log "Формируем фаил списка групп с / ${PROXY_ADDRESS}" "${YELLOW_COLOR}"
curl -s  "${PROXY_ADDRESS}"/ |jq -e -S '.feeds[] |= keys | .feeds[]|.[]'>${FILE_GROUP_ROOT} 2>/dev/null
check_work "Получение списка групп с / завершилось ошибкой!" "Получение списка групп с / завершилось успехом!"

log "Формируем фаил списка групп с /congig/price_snapshot ${PROXY_ADDRESS}" "${YELLOW_COLOR}"
curl -s "${PROXY_ADDRESS}"/config/price_snapshot | jq -s '.[]' | grep -v disable | sed -zr 's/,([^,]*$)/\1/' | jq -S '.|= keys|.[]'>${FILE_GROUP_PRICESNAPSHOT} 2>/dev/null
check_work "Получение списка групп с /congig/price_snapshot завершилось ошибкой!" "Получение списка групп с /congig/price_snapshot завершилось успехом!"

log "Формируем список пересечения / и /congig/price_snapshot у ${PROXY_ADDRESS}" "${YELLOW_COLOR}"
peresech=$(comm -12  <(sort ${FILE_GROUP_ROOT}) <(sort ${FILE_GROUP_PRICESNAPSHOT})|sort -u|tr -d \"|awk '{print "group="$0}'| tr '\n' "|"|sed 's/.$//')

# Создание объедененного некролога за год
for i in {1..12}
do
  (($(curl --silent -I "${DEADPOOL_URL}/${year}/${i}.json" \
    | grep -E "^HTTP" \
    | awk -F " " '{print $2}') == 200)) \
    && curl -s "${DEADPOOL_URL}/${year}/${i}.json" --compressed -o "${i}.json"
done
jq -s 'flatten' /root/*.json > /root/merged_json.json
sed -e '1d' -e '$d' -i /root/merged_json.json

log "Получение хитерного некролога из бакета в s3" "${YELLOW_COLOR}"
data_heater=$(curl -s "${DEADPOOL_URL}/${year}/${month}.json" --compressed )
check_work "Получение некролога из s3 завершилось ошибкой!" "Получение некролога из s3 завершилось успехом!"

# Проверка, что скрипт уже выполнялся
if ! test -f "/root/marker.file"; then
    log "Будем чистить все за год!" "${YELLOW_COLOR}"
    data_heater=$(cat /root/merged_json.json)
    sleep 10
fi

log "Генерация урлов очисти по некрологу " "${YELLOW_COLOR}"
echo "$data_heater" \
| jq -r '.symbols_to_remove[]|@text "price_snapshot/delete?group=" + .group + "&symbol=" + .symbol + "&kind=all" + "&age=10"' \
| grep -Ew "($peresech)" \
| xargs -I url -P 10 -r -n 1 curl -X POST --connect-timeout 5 --max-time 10 --retry 3 --retry-delay 3 --retry-max-time 10 "http://${PROXY_ADDRESS}/url"

log "Завершаю работу." "${YELLOW_COLOR}"
add_metric 0

touch /root/marker.file

