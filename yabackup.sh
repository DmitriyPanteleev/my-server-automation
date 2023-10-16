#!/bin/bash

# Set token
SECRET_TOKEN="secret_token"

# Iterate over files in sync-files.list
while read -r line; do
    FULL_FILE_NAME=${line% *}
    FILE_NAME=$(basename "${line% *}")
    FILE_ACTION=${line##* }
    echo "Full file name: $FULL_FILE_NAME"
    echo "File name: $FILE_NAME"
    echo "File action: $FILE_ACTION"
    # Upload file into Yandex.Disk
    if [ "$FILE_ACTION" == "write" ]; then
        # Get upload URL
        response=$(curl -s -X GET -H "Authorization: OAuth $SECRET_TOKEN" "https://cloud-api.yandex.net/v1/disk/resources/upload?path=/SyncDir/${FILE_NAME}&overwrite=true")
        echo "Response: $response"
        href=$(echo "$response" | jq -r '.href')
        echo "Upload URL: $href"
        # Upload file
        curl -s -X PUT -H "Authorization: OAuth $SECRET_TOKEN" -T "$FULL_FILE_NAME" "$href"
    fi
    # Download file from Yandex.Disk
    if [ "$FILE_ACTION" == "read" ]; then
        # Get download URL
        response=$(curl -s -X GET -H "Authorization: OAuth $SECRET_TOKEN" "https://cloud-api.yandex.net/v1/disk/resources/download?path=/SyncDir/${FILE_NAME}")
        echo "Response: $response"
        href=$(echo "$response" | jq -r '.href')
        echo "Download URL: $href"
        # Download file
        curl -s -o "$FULL_FILE_NAME" -L "$href"
    fi
done < sync-files.list
