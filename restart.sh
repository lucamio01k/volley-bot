#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$ROOT_DIR/data"
PID_FILE="$DATA_DIR/volley-bot.pid"
OUTPUT_FILE="$DATA_DIR/volley-bot.out"
foreground=false

if [[ "${1:-}" == "--foreground" ]]; then
  foreground=true
elif [[ $# -gt 0 ]]; then
  echo "Usage: $0 [--foreground]" >&2
  exit 2
fi

mkdir -p "$DATA_DIR"

if [[ -f "$PID_FILE" ]]; then
  old_pid="$(tr -dc '0-9' < "$PID_FILE")"
  if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
    command_line="$(ps -p "$old_pid" -o command= 2>/dev/null || true)"
    if [[ "$command_line" == *"$ROOT_DIR/main.py"* ]]; then
      echo "Stopping volley bot (PID $old_pid)..."
      kill "$old_pid"
      for _ in {1..50}; do
        if ! kill -0 "$old_pid" 2>/dev/null; then
          break
        fi
        sleep 0.1
      done
    else
      echo "PID file points to an unrelated process; refusing to stop it." >&2
      exit 1
    fi
  fi
fi

if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  python_bin="$ROOT_DIR/.venv/bin/python"
elif [[ -x "$ROOT_DIR/venv/bin/python" ]]; then
  python_bin="$ROOT_DIR/venv/bin/python"
else
  echo "Virtual environment not found. Create .venv and install requirements.txt." >&2
  exit 1
fi

cd "$ROOT_DIR"
if [[ "$foreground" == true ]]; then
  echo "Starting volley bot in foreground..."
  printf '%s\n' "$$" > "$PID_FILE"
  exec "$python_bin" "$ROOT_DIR/main.py"
fi

echo "Starting volley bot..."
nohup "$python_bin" "$ROOT_DIR/main.py" >>"$OUTPUT_FILE" 2>&1 &
new_pid=$!
printf '%s\n' "$new_pid" > "$PID_FILE"
sleep 1

if ! kill -0 "$new_pid" 2>/dev/null; then
  echo "The bot stopped during startup. See $OUTPUT_FILE" >&2
  exit 1
fi

echo "Volley bot running with PID $new_pid"
echo "Output: $OUTPUT_FILE"
