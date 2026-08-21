import subprocess
import os
import requests
from pathlib import Path
from gpu_utils import pick_available_gpu
from pod import Pod, podStatus

# --- config ---
RUNPOD_ENDPOINT = "..."      # your pod's exposed URL
MODEL_NAME = "..."           # fixed, single model
INPUT_DIR = "./PDFTesting"
JAR_PATH = "./pdffigures2/pdffigures2.jar"
EXTRACT_OUT_DIR = "./pdffigures2_out"
RESULTS_PATH = "./alt_text_results.json"   # or .csv — decide below
RUNPOD_API_KEY = ""  # bug fix: was hardcoded — never commit real keys
HF_TOKEN = ""
GPU_CANDIDATES = ["NVIDIA A40", "NVIDIA RTX A6000", "NVIDIA L40S"]  # bug fix: was "L40s" (wrong case)

RUNPOD_POD_CONFIG = {
    "name": "internvl3.5-8b-pod",
    "imageName": "vllm/vllm-openai:v0.10.1",
    # gpuTypeIds is set dynamically at creation time — see start_new_pod()
    "gpuCount": 1,
    "containerDiskInGb": 30,
    "volumeInGb": 5,
    "ports": ["8000/http"],
    "dockerStartCmd": [
        "OpenGVLab/InternVL3_5-8B",
        "--trust-remote-code",
        "--host", "0.0.0.0",
        "--port", "8000",
    ],
    "env": {
        "HF_TOKEN": HF_TOKEN
    },
}


# <----Pod lifecycle orchestration----->

def start_new_pod(api_key: str) -> Pod:
    """Picks an available GPU, then creates and waits for a new pod."""
    best_gpu = pick_available_gpu(GPU_CANDIDATES, api_key)
    if best_gpu is None:
        raise RuntimeError(f"No available GPU among candidates: {GPU_CANDIDATES}")

    print(f"Selected GPU: {best_gpu}")

    payload = dict(RUNPOD_POD_CONFIG)  # shallow copy — don't mutate the module-level config
    payload["gpuTypeIds"] = [best_gpu]

    pod = Pod(pod_name=payload["name"], pod_status=podStatus.UNKNOWN)
    pod.start_pod(payload, api_key)
    pod.wait_for_pod(api_key)

    return pod


# <----configChecks----->

def configChecks():
    result = subprocess.run(["colima", "status"], capture_output=True, text=True)
    if result.returncode == 1:
        print("Colima is not running. Please start Colima and try again.")
        return "Colima not running"

    result = subprocess.run(["ls", " pdffigures2/pdffigures2.jar"], capture_output=True, text=True)
    if result.returncode == 0:
        print("pdffigures2.0 jar file has not been created, check step 3 in SETUP.md ")
        return "pdffigures2.0 jar not created"

    return "Valid"


def run_extraction():
    PROJECT_ROOT = Path(__file__).resolve().parent
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{PROJECT_ROOT}:/work",
        "-w", "/work",
        "--entrypoint", "bash",
        "pdffigures2-builder",
        "-c", "python3 pdf_batch_runner.py --input-dir ./PDFTesting --jar ./pdffigures2/pdffigures2.jar --output-dir ./pdffigures2_out --dpi 300 --java-heap 6g -v",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)

    print(result.stderr)
    if result.returncode != 0:
        print("EXTRACTION FAILED. SEE LOGS ABOVE")
        return False

    return True


def load_manifest():
    """Read manifest.csv, return list of row dicts."""
    ...

def load_existing_results():
    """Read RESULTS_PATH if it exists, return dict keyed by image_path."""
    ...

def call_model(row):
    """POST one image to RunPod, return parsed alt-text + usage stats."""
    ...

def call_with_retry(row, max_attempts=3):
    """Wrap call_model with retry/backoff. Raise on exhausted attempts."""
    ...

def save_result(row, result):
    """Append/update one result in RESULTS_PATH."""
    ...


def main():
    configStatus = configChecks()
    if (configStatus == "pdffigures2.0 jar not created"):
        if not run_extraction():
            return
    elif configStatus == "Colima not running":
        return 

    pod = start_new_pod(RUNPOD_API_KEY)
    print(f"Pod ready: {pod.pod_id} — status {pod.pod_status}")

    manifest_rows = load_manifest()
    existing = load_existing_results()

    for row in manifest_rows:
        if row["image_path"] in existing:
            continue
        try:
            result = call_with_retry(row)
            save_result(row, result)
        except Exception as e:
            print(f"Failed on {row.get('image_path')}: {e}")


if __name__ == "__main__":
    main()