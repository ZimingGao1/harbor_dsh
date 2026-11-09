#!/usr/bin/env bash
# 收集 agent 超时任务（AgentTimeoutError + reward 0），生成放开超时的重跑目录
set -euo pipefail
JOB="${1:?usage: collect-timeouts.sh <job-dir> [src-tasks-dir] [dst-dir]}"
SRC="${2:-tasks}"
DST="${3:-retry-timeout}"
rm -rf "$DST"
mkdir -p "$DST"
n=0
for d in "$JOB"/*/; do
  [ -d "$d" ] || continue
  task=$(basename "$d" | cut -d_ -f1)
  rw="$d/verifier/reward.txt"
  [ -f "$rw" ] || continue
  [ "$(cat "$rw")" = "0" ] || continue
  if grep -q "AgentTimeoutError" "$d/exception.txt" 2>/dev/null; then
    if [ -d "$SRC/$task" ]; then
      cp -r "$SRC/$task" "$DST/$task"
      # 放开 agent 超时（900 -> 7200）
      sed -i 's/^timeout_sec = 900\.0$/timeout_sec = 7200.0/' "$DST/$task/task.toml"
      n=$((n+1))
      echo "  $task (agent timeout -> 7200s)"
    fi
  fi
done
echo "共 $n 个超时任务"
grep -h "timeout_sec" "$DST"/*/task.toml 2>/dev/null | sort | uniq -c || true
