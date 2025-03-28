#!/bin/bash

MAX_JOBS=4
TASK_QUEUE=()

# Функция добавления задач в очередь
add_task() {
  TASK_QUEUE+=("$1")
}

# Функция выполнения задач из очереди
process_queue() {
  for task_name in "${TASK_QUEUE[@]}"; do
    wait_for_jobs
    task "$task_name" &
  done
}

# Добавляем задачи в очередь
add_task "Task1"
add_task "Task2"
add_task "Task3"
add_task "Task4"
add_task "Task5"

# Обрабатываем очередь
process_queue
wait
echo "Dynamic queue processing completed!"
