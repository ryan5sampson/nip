#!/bin/sh
# Start NIP in the background and open it, restarting any NIP already running so code changes load.
cd "$(dirname "$0")" || exit 1
lsof -ti tcp:8765 -sTCP:LISTEN | xargs kill 2>/dev/null
sleep 1
mkdir -p ~/NIP
PYTHONUNBUFFERED=1 nohup .venv/bin/nip > ~/NIP/nip.log 2>&1 &
