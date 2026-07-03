#!/bin/bash
# Local trace visualizer for Claude Code + Codex sessions.
cd "$(dirname "$0")"

PORT=3331
LOG=/tmp/trace-visualizer.log

# Kill any existing process on the port (force-kill if it lingers).
PID=$(lsof -t -i :$PORT 2>/dev/null)
if [ -n "$PID" ]; then
    echo "Killing existing process on port $PORT (PID: $PID)"
    kill $PID 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        lsof -t -i :$PORT >/dev/null 2>&1 || break
        sleep 0.3
    done
    if lsof -t -i :$PORT >/dev/null 2>&1; then
        kill -9 $(lsof -t -i :$PORT) 2>/dev/null
        sleep 0.5
    fi
fi

# Prefer uv's env, but fall back to plain python3 if uv is missing/broken.
if command -v uv >/dev/null 2>&1 && uv run --no-sync python -c '' >/dev/null 2>&1; then
    RUN="uv run --no-sync python"
else
    RUN="python3"
fi

nohup $RUN visualizer/server.py --port $PORT > "$LOG" 2>&1 &
SERVER_PID=$!

# Wait until the API actually answers before declaring victory.
for _ in $(seq 1 30); do
    if curl -sf -o /dev/null "http://localhost:$PORT/api/sessions"; then
        echo "Trace visualizer running at http://localhost:$PORT/ (PID: $SERVER_PID, log: $LOG)"
        exit 0
    fi
    if ! kill -0 $SERVER_PID 2>/dev/null; then
        break
    fi
    sleep 0.3
done

echo "ERROR: server failed to start; last log lines:" >&2
tail -20 "$LOG" >&2
exit 1
