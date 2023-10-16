#!/bin/bash

# Set token
SECRET_TOKEN="secret_token"

# Iterate over files in sync-files.list
# Format: <file_name> <action>
# Action: read or write
# This list placed in the same directory as script
while read -r line; do
    # Prasing line
    FULL_FILE_NAME=${line% *}
    FILE_NAME=$(basename "${line% *}")
    FILE_ACTION=${line##* }
    # Upload file into Yandex.Disk
    if [ "$FILE_ACTION" == "write" ]; then
        # Get upload URL
        href=$(curl -s -X GET -H "Authorization: OAuth $SECRET_TOKEN" "https://cloud-api.yandex.net/v1/disk/resources/upload?path=/SyncDir/${FILE_NAME}&overwrite=true" | jq -r '.href')
        # Upload file
        curl -s -X PUT -H "Authorization: OAuth $SECRET_TOKEN" -T "$FULL_FILE_NAME" "$href"
    fi
    # Download file from Yandex.Disk
    if [ "$FILE_ACTION" == "read" ]; then
        # Get download URL
        href=$(curl -s -X GET -H "Authorization: OAuth $SECRET_TOKEN" "https://cloud-api.yandex.net/v1/disk/resources/download?path=/SyncDir/${FILE_NAME}" | jq -r '.href')
        # Download file
        curl -s -o "$FULL_FILE_NAME" -L "$href"
    fi
done < sync-files.list
