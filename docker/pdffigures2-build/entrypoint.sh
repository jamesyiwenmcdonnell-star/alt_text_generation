#!/usr/bin/env bash
# Entrypoint for the running pipeline daemon: api_server.py (intake API) and
# worker.py (job queue processor) as two independent foreground processes in
# one container. Kept separate rather than running the worker loop as a
# background task inside the API's event loop -- extraction/pod-wait/
# generation are long synchronous calls that would otherwise starve status
# requests if they shared one process.
#
# `wait -n` exits (taking the container down with it) as soon as either
# process dies, so `docker run --restart unless-stopped` notices and
# restarts the whole thing rather than silently running with just one half up.
set -euo pipefail

uvicorn api_server:app --host 0.0.0.0 --port 8000 &
python3 worker.py &
wait -n
