#!/usr/bin/env python3
"""
startup.py

Host-side entry point for the alt-text pipeline. Runs the checks that only
make sense on the host -- Colima up, pdffigures2.jar built -- fixing either
one automatically if needed, then launches the pdffigures2-builder container
as a persistent daemon running api_server.py (the PDF intake API) and
worker.py (the job queue processor) side by side.

Usage:
    python3 startup.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
PDFFIGURES2_SRC = PROJECT_ROOT / "pdffigures2"
JAR_PATH = PDFFIGURES2_SRC / "pdffigures2.jar"
BUILD_SCRIPT = PROJECT_ROOT / "docker" / "pdffigures2-build" / "build.sh"
DOCKER_BUILD_CONTEXT = PROJECT_ROOT / "docker" / "pdffigures2-build"
ENTRYPOINT_SCRIPT = "docker/pdffigures2-build/entrypoint.sh"  # relative to /work inside the container
IMAGE_NAME = "pdffigures2-builder"
CONTAINER_NAME = "alttext-pipeline"
HOST_PORT = int(os.environ.get("PIPELINE_PORT", "8000"))

# Forwarded into the container so controller.py/worker.py's os.environ.get(...)
# calls pick them up automatically -- none of these are ever printed or logged here.
FORWARDED_ENV_VARS = ("RUNPOD_API_KEY", "HF_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


def ensure_colima_running() -> None:
    result = subprocess.run(["colima", "status"], capture_output=True, text=True)
    if result.returncode == 0:
        print("==> Colima already running")
        return

    print("==> Colima not running -- starting it (colima start --cpu 4 --memory 8)")
    subprocess.run(["colima", "start", "--cpu", "4", "--memory", "8"], check=True)
    print("==> Colima started")


def ensure_jar_built() -> None:
    if JAR_PATH.exists():
        print(f"==> {JAR_PATH} already built")
        return

    # Checks for build.sbt, not just the directory: an empty pdffigures2/ passes
    # an is_dir() check but sends sbt into a bare /build, where it fails with the
    # unhelpful "Neither build.sbt nor a 'project' directory in the current
    # directory" instead of telling you the clone is missing. `git clone` into an
    # existing empty directory works, so there's nothing to remove first.
    if not (PDFFIGURES2_SRC / "build.sbt").is_file():
        raise RuntimeError(
            f"{PDFFIGURES2_SRC} is missing or empty (no build.sbt) -- clone it first:\n"
            f"    git clone https://github.com/allenai/pdffigures2.git {PDFFIGURES2_SRC}\n"
            f"then apply the bintray build fixes documented in SETUP.md before re-running."
        )

    print(f"==> {JAR_PATH} missing -- building it via build.sh (can take several minutes on first run)")
    subprocess.run(["bash", str(BUILD_SCRIPT)], check=True, cwd=PROJECT_ROOT)

    if not JAR_PATH.exists():
        raise RuntimeError(f"build.sh finished but {JAR_PATH} still doesn't exist -- check the build output above")


def ensure_image_built() -> None:
    """Always runs `docker build` -- deliberately does NOT skip when the image
    already exists. An existing image says nothing about whether it matches the
    current Dockerfile, and the failure mode of using a stale one is delayed and
    confusing: the container comes up fine and dies later on a
    ModuleNotFoundError for a package added to the Dockerfile's pip3 install
    line after the image was first cached (this bit us with both `requests` and
    `fastapi`). Docker's own layer cache already makes the unchanged case a
    couple of seconds and a no-op, so there is nothing to gain by guessing
    freshness ourselves."""
    print(f"==> building {IMAGE_NAME} image (no-op if the Dockerfile hasn't changed)")
    subprocess.run(["docker", "build", "-t", IMAGE_NAME, str(DOCKER_BUILD_CONTEXT)], check=True)


def _existing_container_status() -> str | None:
    """Returns the container's docker status ('running', 'exited', ...), or
    None if no container with this name exists at all."""
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Status}}", CONTAINER_NAME],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def run_pipeline_daemon() -> int:
    """Runs api_server.py + worker.py as a persistent, restart-on-crash
    container (see entrypoint.sh) -- replaces the old one-shot
    `docker run --rm ... controller.py`. Safe to re-run: does nothing if the
    daemon is already up, replaces it if it exists but isn't running."""
    status = _existing_container_status()
    if status == "running":
        print(f"==> {CONTAINER_NAME} already running")
    else:
        if status is not None:
            print(f"==> found a stopped {CONTAINER_NAME} container ({status}) -- removing it first")
            subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], check=True)

        env_args = []
        for key in FORWARDED_ENV_VARS:
            value = os.environ.get(key)
            if value:
                env_args += ["-e", f"{key}={value}"]
            else:
                print(f"==> warning: {key} not set on host -- the pipeline will fail its startup env check")

        cmd = [
            "docker", "run", "-d",
            "--name", CONTAINER_NAME,
            "--restart", "unless-stopped",
            "-v", f"{PROJECT_ROOT}:/work",
            "-w", "/work",
            "-p", f"{HOST_PORT}:8000",
            "-e", "PYTHONUNBUFFERED=1",  # without a tty, python fully-buffers stdout --
                                          # nothing would show up in `docker logs` until the buffer fills
            *env_args,
            "--entrypoint", "bash",
            IMAGE_NAME,
            ENTRYPOINT_SCRIPT,
        ]
        print("==> starting pipeline daemon (api_server.py + worker.py)")
        subprocess.run(cmd, check=True)

    print(f"==> intake API: http://localhost:{HOST_PORT}/docs")
    print(f"==> logs: docker logs -f {CONTAINER_NAME}")
    return 0


def main() -> int:
    try:
        ensure_colima_running()
        ensure_jar_built()
        ensure_image_built()
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"startup.py: {exc}", file=sys.stderr)
        return 1

    return run_pipeline_daemon()


if __name__ == "__main__":
    sys.exit(main())
