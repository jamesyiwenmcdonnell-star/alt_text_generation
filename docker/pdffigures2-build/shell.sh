#!/usr/bin/env bash
# Drop into an interactive shell inside the pdffigures2-builder container (JDK 17 +
# Python 3 + pikepdf), with your whole project mounted at /work. Once inside, run
# the exact same commands you've been running on the host -- `java -jar`,
# `python3 pdf_batch_runner.py ...`, `python3 diagnose_tagging.py ...` -- just from
# inside the container instead of on the Mac. No path translation needed since your
# whole project directory is what's mounted.
#
# For PDFs with JPEG2000-encoded images, add pdf_batch_runner.py's
# --extra-classpath flag pointing at the image's baked-in JAI ImageIO plugin
# jars (path is in $JAI_JPEG2000_CLASSPATH once you're inside the shell), e.g.:
#   python3 pdf_batch_runner.py --extra-classpath "$JAI_JPEG2000_CLASSPATH" --input-dir ./PDFTesting
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

# Built unconditionally on purpose: an existing image tells you nothing about
# whether it matches the current Dockerfile, and skipping on "image exists"
# used to hand you a stale image that only failed much later (e.g. a
# ModuleNotFoundError for a package added to the Dockerfile's pip3 install
# line). Docker's layer cache makes the unchanged case a fast no-op.
echo "==> building $IMAGE_NAME image (no-op if the Dockerfile hasn't changed)"
docker build -t "$IMAGE_NAME" "$SCRIPT_DIR"

echo "==> mounting $PROJECT_ROOT at /work -- run your usual commands from there"
docker run --rm -it \
  -v "$PROJECT_ROOT:/work" \
  -w /work \
  --entrypoint bash \
  "$IMAGE_NAME"