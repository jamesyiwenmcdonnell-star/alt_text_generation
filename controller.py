import subprocess
import os
import requests
from pathlib import Path
from gpu_utils import pick_available_gpu
from pod import Pod, podStatus, list_pods, terminate_pod_by_id

# --- config ---
RUNPOD_ENDPOINT = "..."      # your pod's exposed URL
MODEL_NAME = "..."           # fixed, single model
INPUT_DIR = "./PDFTesting"
JAR_PATH = "./pdffigures2/pdffigures2.jar"
EXTRACT_OUT_DIR = "./pdffigures2_out"
RESULTS_PATH = "./alt_text_results.json"   # or .csv — decide below
RUNPOD_API_KEY = " " # bug fix: was hardcoded — never commit real keys
HF_TOKEN = " "

RUNPOD_POD_CONFIG = {
    "name": "internvl3.5-8b-pod",
    "imageName": "vllm/vllm-openai:v0.10.1",
    # gpuTypeIds is set dynamically at creation time — see start_new_pod()
    "gpuCount": 1,
    "containerDiskInGb": 30,
    "volumeInGb": 5,
    "ports": ["8000/http"],
    "dockerStartCmd": [
        # v0.10.1's entrypoint is `python3 -m vllm.entrypoints.openai.api_server`,
        # which has no positional model argument -- --model is required. (Only
        # v0.11+'s `vllm serve` entrypoint accepts a bare model name.)
        "--model", "OpenGVLab/InternVL3_5-8B",
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
    best_gpu = pick_available_gpu(api_key)
    if best_gpu is None:
        raise RuntimeError("No available GPU among the candidates in gpu_ids_snapshot.txt")

    print(f"Selected GPU: {best_gpu}")

    payload = dict(RUNPOD_POD_CONFIG)  # shallow copy — don't mutate the module-level config
    payload["gpuTypeIds"] = [best_gpu]

    pod = Pod(pod_name=payload["name"], pod_status=podStatus.UNKNOWN)
    pod.start_pod(payload, api_key)
    pod.wait_for_pod(api_key)

    return pod


def stop_pod(pod: Pod, api_key: str) -> None:
    """Stops a running pod without deleting it -- storage is preserved and it
    can be restarted later. Use this over terminate_pod() when you just want
    to pause billing for compute between runs."""
    pod.stop_pod(api_key)
    print(f"Pod stopped: {pod.pod_id} — status {pod.pod_status}")


def terminate_pod(pod: Pod, api_key: str) -> None:
    """Permanently deletes a pod and its storage -- not reversible."""
    pod.terminate_pod(api_key)
    print(f"Pod terminated: {pod.pod_id} — status {pod.pod_status}")


def terminate_all_pods(api_key: str) -> None:
    """Terminates every pod on the account that's currently RUNNING or EXITED
    (stopped) -- skips pods already TERMINATED. Useful for cleaning up stray
    pods left over from a crashed or interrupted run."""
    pods = list_pods(api_key)
    live_pods = [p for p in pods if p.get("desiredStatus") in ("RUNNING", "EXITED")]

    if not live_pods:
        print("No running or stopped pods found.")
        return

    for p in live_pods:
        pod_id = p["id"]
        print(f"Terminating {pod_id} ({p.get('name', '?')}) — was {p.get('desiredStatus')}")
        try:
            terminate_pod_by_id(pod_id, api_key)
            print("  terminated")
        except requests.RequestException as e:
            print(f"  FAILED: {e}")



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
    print("INFO: Application will terminate all pods currently running or paused")
    terminate_all_pods(RUNPOD_API_KEY)
    pod = start_new_pod(RUNPOD_API_KEY)
    print(f"Pod ready: {pod.pod_id} — status {pod.pod_status}")
    print(f"Stopping pod")
    stop_pod(pod, RUNPOD_API_KEY)
    print(f"Terminating pod")
    terminate_pod(pod, RUNPOD_API_KEY)

    """
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
    """

if __name__ == "__main__":
    main()