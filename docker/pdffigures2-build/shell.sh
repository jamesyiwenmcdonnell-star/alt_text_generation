#!/usr/bin/env bash
# Drop into an interactive shell inside the pdffigures2-builder container (JDK 17 +
# Python 3 + pikepdf), with your whole project mounted at /work. Once inside, run
# the exact same commands you've been running on the host -- `java -jar`,
# `python3 pdf_batch_runner.py ...`, `python3 diagnose_tagging.py ...` -- just from
# inside the container instead of on the Mac. No path translation needed since your
# whole project directory is what's mounted.
#
# Usage (from your project root, e.g. internVL/):
#   ./docker/pdffigures2-build/shell.sh
#
# Or point it at a different project root:
#   ./docker/pdffigures2-build/shell.sh /path/to/project

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${1:-$(pwd)}"
IMAGE_NAME="pdffigures2-builder"

if [ ! -d "$PROJECT_ROOT" ]; then
  echo "error: '$PROJECT_ROOT' not found." >&2
  exit 1
fi
PROJECT_ROOT="$(cd "$PROJECT_ROOT" && pwd)"

# NOTE: if you already built this image before python3/pikepdf were added to the
# Dockerfile, this check finds the OLD cached image and won't pick up the change --
# run `docker build -t pdffigures2-builder ./docker/pdffigures2-build` yourself once
# after updating the Dockerfile, don't rely on this auto-build skipping stale images.
if ! docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
  echo "==> building $IMAGE_NAME image (one-time, cached after this)"
  docker build -t "$IMAGE_NAME" "$SCRIPT_DIR"
fi

echo "==> mounting $PROJECT_ROOT at /work -- run your usual commands from there"
docker run --rm -it \
  -v "$PROJECT_ROOT:/work" \
  -w /work \
  --entrypoint bash \
  "$IMAGE_NAME"