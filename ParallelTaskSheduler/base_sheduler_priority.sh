#!/bin/bash

MAX_JOBS=3
PRIORITY_TASKS=("High1" "High2")
NORMAL_TASKS=("Normal1" "Normal2" "Normal3")

wait_for_jobs() {
  while (( $(jobs -r | wc -l) >= MAX_JOBS )); do
    sleep 1
  done
}

task() {
  local name=$1
  echo "Task $name started"
  sleep $((RANDOM % 5 + 2))
  echo "Task $name completed"
}

# Выполнение задач с приоритетом
for task_name in "${PRIORITY_TASKS[@]}" "${NORMAL_TASKS[@]}"; do
  wait_for_jobs
  task "$task_name" &
done

wait
echo "All prioritized tasks completed!"
