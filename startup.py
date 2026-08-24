#!/usr/bin/env python3
"""
startup.py

Host-side entry point for the alt-text pipeline. Runs the checks that only
make sense on the host -- Colima up, pdffigures2.jar built -- fixing either
one automatically if needed, then launches the pdffigures2-builder container
and runs controller.py inside it. Replaces the old "run shell.sh, then
manually run controller.py from the interactive shell" flow with one command.

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
IMAGE_NAME = "pdffigures2-builder"

# Forwarded into the container so controller.py's os.environ.get(...) picks
# them up automatically -- neither is ever printed or logged here.
FORWARDED_ENV_VARS = ("RUNPOD_API_KEY", "HF_TOKEN")


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

    if not PDFFIGURES2_SRC.is_dir():
        raise RuntimeError(
            f"{PDFFIGURES2_SRC} not found -- clone it first: "
            f"git clone https://github.com/allenai/pdffigures2.git {PDFFIGURES2_SRC}"
        )

    print(f"==> {JAR_PATH} missing -- building it via build.sh (can take several minutes on first run)")
    subprocess.run(["bash", str(BUILD_SCRIPT)], check=True, cwd=PROJECT_ROOT)

    if not JAR_PATH.exists():
        raise RuntimeError(f"build.sh finished but {JAR_PATH} still doesn't exist -- check the build output above")


def ensure_image_built() -> None:
    result = subprocess.run(["docker", "image", "inspect", IMAGE_NAME], capture_output=True, text=True)
    if result.returncode == 0:
        return
    print(f"==> building {IMAGE_NAME} image (one-time, cached after this)")
    subprocess.run(["docker", "build", "-t", IMAGE_NAME, str(DOCKER_BUILD_CONTEXT)], check=True)


def run_controller_in_container() -> int:
    """Runs controller.py inside pdffigures2-builder, the same image/mount
    layout shell.sh drops you into, but non-interactively and with the host's
    RunPod/HF credentials forwarded in."""
    env_args = []
    for key in FORWARDED_ENV_VARS:
        value = os.environ.get(key)
        if value:
            env_args += ["-e", f"{key}={value}"]
        else:
            print(f"==> warning: {key} not set on host -- controller.py will run without it")

    cmd = [
        "docker", "run", "--rm",
        "-v", f"{PROJECT_ROOT}:/work",
        "-w", "/work",
        "-e", "PYTHONUNBUFFERED=1",  # without a tty, python fully-buffers stdout --
                                      # nothing would show up until the buffer fills
        *env_args,
        "--entrypoint", "python3",
        IMAGE_NAME,
        "controller.py",
    ]
    print("==> starting container and running controller.py")
    return subprocess.run(cmd).returncode


def main() -> int:
    try:
        ensure_colima_running()
        ensure_jar_built()
        ensure_image_built()
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"startup.py: {exc}", file=sys.stderr)
        return 1

    return run_controller_in_container()


if __name__ == "__main__":
    sys.exit(main())
