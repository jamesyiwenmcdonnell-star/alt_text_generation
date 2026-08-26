#!/usr/bin/env bash
# Build pdffigures2's assembly jar inside a JDK-17 Docker container -- no Java
# needed on the host at all, every time.
#
# Usage (from your project root, e.g. AltTextEmbedding/):
#   ./docker/pdffigures2-build/build.sh [path-to-pdffigures2-checkout]
#
# Defaults to ./pdffigures2 if no path given. First run: `git clone
# https://github.com/allenai/pdffigures2.git` into your project folder.
#
# Caches sbt/ivy2/coursier dependency downloads in a docker volume so repeat
# builds (e.g. after editing FigureExtractionTemplate.scala) don't redownload
# the world every time.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="${1:-./pdffigures2}"
IMAGE_NAME="pdffigures2-builder"
CACHE_VOLUME_ROOT="pdffigures2-cache"

# Tests for build.sbt rather than just the directory -- an empty checkout dir
# passes a -d test and then fails deep inside sbt with "Neither build.sbt nor a
# 'project' directory", which says nothing about the real problem.
if [ ! -f "$SOURCE_DIR/build.sbt" ]; then
  echo "error: '$SOURCE_DIR' is missing or empty (no build.sbt)." >&2
  echo "clone it first: git clone https://github.com/allenai/pdffigures2.git $SOURCE_DIR" >&2
  echo "then apply the bintray build fixes documented in SETUP.md." >&2
  exit 1
fi
SOURCE_DIR="$(cd "$SOURCE_DIR" && pwd)"

# Unconditional -- see the note in shell.sh: skipping when the image already
# exists silently reuses one built from an older Dockerfile. Docker's layer
# cache makes this a fast no-op when nothing changed.
echo "==> building $IMAGE_NAME image (no-op if the Dockerfile hasn't changed)"
docker build -t "$IMAGE_NAME" "$SCRIPT_DIR"

# Three separate volumes -- coursier/ivy2/sbt each own their whole mount root,
# so sharing one volume across all three would risk top-level name collisions.
for suffix in coursier ivy2 sbt; do
  docker volume create "${CACHE_VOLUME_ROOT}-${suffix}" >/dev/null
done

echo "==> running sbt assembly against $SOURCE_DIR"
docker run --rm \
  -v "$SOURCE_DIR:/build" \
  -v "${CACHE_VOLUME_ROOT}-coursier:/root/.cache/coursier" \
  -v "${CACHE_VOLUME_ROOT}-ivy2:/root/.ivy2" \
  -v "${CACHE_VOLUME_ROOT}-sbt:/root/.sbt" \
  "$IMAGE_NAME"

# pdffigures2's build.sbt pins `assembly / assemblyOutputPath := file("pdffigures2.jar")`,
# which resolves relative to the project root -- NOT sbt-assembly's default
# target/scala-2.12/<name>-assembly-<version>.jar naming. Check the known path
# first, fall back to a search in case a future revision changes that setting.
JAR="$SOURCE_DIR/pdffigures2.jar"
if [ ! -f "$JAR" ]; then
  JAR="$(find "$SOURCE_DIR" -maxdepth 2 -name '*.jar' -newer "$SOURCE_DIR/build.sbt" 2>/dev/null | head -n1)"
fi

if [ -n "$JAR" ] && [ -f "$JAR" ]; then
  echo "==> built: $JAR"
else
  echo "==> build finished but no jar found -- check output above for errors" >&2
  exit 1
fi