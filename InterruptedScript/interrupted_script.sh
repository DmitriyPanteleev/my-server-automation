#!/bin/bash

# Имя файла-снапшота
SNAPSHOT_FILE="progress.snapshot"

# Список задач (например, файлы для обработки)
FILES=("file1.txt" "file2.txt" "file3.txt" "file4.txt" "file5.txt")

# Чтение текущего прогресса из снапшота
CURRENT_INDEX=0
if [[ -f $SNAPSHOT_FILE ]]; then
  CURRENT_INDEX=$(<"$SNAPSHOT_FILE")
  echo "Прогресс найден: начнем с файла ${FILES[$CURRENT_INDEX]}"
else
  echo "Снапшот не найден: начнем сначала"
fi

# Обработка файлов
for ((i=CURRENT_INDEX; i<${#FILES[@]}; i++)); do
  echo "Обрабатываю ${FILES[$i]}..."
  
  # Имитация долгой обработки
  sleep 2
  
  echo "Файл ${FILES[$i]} обработан!"
  
  # Обновление прогресса в файле-снапшоте
  echo $((i + 1)) > "$SNAPSHOT_FILE"
done

# Удаление снапшота после завершения работы
rm -f "$SNAPSHOT_FILE"
echo "Все файлы обработаны, прогресс очищен!"
