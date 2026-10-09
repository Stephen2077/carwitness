#!/usr/bin/env bash
# Start the VSS+LLM mock (:9100) and the CarWitness app (:8080) for local testing. Ctrl+C stops both.
#   dev/run_local.sh            -> http://127.0.0.1:8080/  (also works at /app/)
set -euo pipefail
cd "$(dirname "$0")/.."
MOCK_PORT="${MOCK_PORT:-9100}"
APP_PORT="${PORT:-8080}"

# Optional: a real playable clip for /videos/stream (dev/sample.mp4 ships with the repo).
if [ ! -f dev/sample.mp4 ] && command -v ffmpeg >/dev/null 2>&1; then
  ffmpeg -loglevel error -f lavfi -i testsrc=duration=5:size=640x360:rate=15 -pix_fmt yuv420p \
         -movflags +faststart dev/sample.mp4
fi

MOCK_PORT="$MOCK_PORT" python3 dev/mock_server.py &
MOCK_PID=$!
trap 'kill $MOCK_PID 2>/dev/null || true' EXIT INT TERM
sleep 0.5

VSS_URL="http://127.0.0.1:$MOCK_PORT" VSS_USERNAME="${VSS_USERNAME:-demo}" VSS_PASSWORD="${VSS_PASSWORD:-demo}" \
WANDB_API_KEY="${WANDB_API_KEY:-mock-key}" WANDB_TEAM="${WANDB_TEAM:-team}" WANDB_PROJECT="${WANDB_PROJECT:-carwitness}" \
LLM_BASE_URL="http://127.0.0.1:$MOCK_PORT/v1" PORT="$APP_PORT" \
python3 app/main.py
