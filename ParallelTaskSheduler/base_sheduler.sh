#!/bin/bash

# Максимальное количество фоновых задач
MAX_JOBS=4

# Функция для ожидания завершения задач
wait_for_jobs() {
  while (( $(jobs -r | wc -l) >= MAX_JOBS )); do
    sleep 1
  done
}

# Функция, которая будет выполняться в фоне
task() {
  local id=$1
  echo "Task $id started"
  sleep $((RANDOM % 5 + 1))  # Имитация выполнения
  echo "Task $id completed"
}

# Основной цикл
for i in {1..10}; do
  wait_for_jobs  # Ожидание освобождения очереди
  task "$i" &    # Запуск задачи в фоне
done

# Ожидание завершения всех задач
wait
echo "All tasks completed!"
