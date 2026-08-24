import subprocess
import os
import requests
from pathlib import Path
from gpu_utils import pick_available_gpu
from pod import Pod, podStatus, list_pods, terminate_pod_by_id
from runpod_VL import generate_alt_text
from pdf_batch_runner import extract_images
import logging


# --- config ---
RUNPOD_ENDPOINT = "..."      # your pod's exposed URL
MODEL_NAME = "..."           # fixed, single model
INPUT_DIR = "./PDFTesting"
JAR_PATH = "./pdffigures2/pdffigures2.jar"
EXTRACT_OUT_DIR = "./pdffigures2_out"
RESULTS_PATH = "./alt_text_results.json"   # or .csv — decide below
POD_STATE_PATH = "./pod_state.txt"
RUNPOD_API_KEY = ""
HF_TOKEN = ""

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


def _parse_optional(value: str | None) -> str | None:
    return None if value in (None, "", "None") else value


def save_pod(pod: Pod, path: str = POD_STATE_PATH) -> None:
    """Persists a pod's identifying fields to a text file, so a later run
    (e.g. after this process crashed or was interrupted) can recover it --
    to check on it, stop it, or terminate it -- without needing to know its
    id ahead of time."""
    lines = [
        f"pod_name={pod.pod_name}",
        f"pod_id={pod.pod_id}",
        f"port={pod.port}",
        f"pod_status={pod.pod_status.name}",
    ]
    Path(path).write_text("\n".join(lines) + "\n")


def load_pod(path: str = POD_STATE_PATH) -> Pod | None:
    """Reconstructs a Pod from a file written by save_pod(). Returns None if
    no saved state exists. Note the recovered pod_status reflects whatever it
    was at save time -- call pod.app_startup_pod_checker(api_key) to refresh it."""
    state_file = Path(path)
    if not state_file.exists():
        return None

    fields: dict[str, str] = {}
    for line in state_file.read_text().splitlines():
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        fields[key] = value

    port = _parse_optional(fields.get("port"))
    return Pod(
        pod_name=fields.get("pod_name"),
        pod_status=podStatus[fields["pod_status"]],
        pod_id=_parse_optional(fields.get("pod_id")),
        port=int(port) if port is not None else None,
    )


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

def extract_pdf_images(skip_done: bool = True) -> dict:
    """Runs pdffigures2 extraction over INPUT_DIR, writing manifest.csv,
    validation_report.csv, and the figures/ folder under EXTRACT_OUT_DIR --
    wraps pdf_batch_runner.extract_images() with this project's own paths so
    callers don't need to know them. Raises FileNotFoundError if the jar
    hasn't been built yet (see SETUP.md)."""
    print(f"INFO: Extracting figures from PDFs in {INPUT_DIR} ...")
    result = extract_images(
        input_dir=INPUT_DIR,
        jar_path=JAR_PATH,
        output_dir=EXTRACT_OUT_DIR,
        skip_done=skip_done,
    )
    print(
        f"INFO: Extraction done: {result['pdfs_found']} PDF(s) found under {INPUT_DIR}, "
        f"{result['pdfs_processed']} processed this run -> {result['figures_dir']}"
    )
    return result


def generate_text(pod_id, runpod_api_key):
    generate_alt_text(pod_id, runpod_api_key, image_dir=os.path.join(EXTRACT_OUT_DIR, "figures"))
    print("INFO: Alt text generated")


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
    """
    print("INFO: Application will terminate all pods currently running or paused")
    terminate_all_pods(RUNPOD_API_KEY)
    pod = start_new_pod(RUNPOD_API_KEY)
    save_pod(pod) 
    """                           

    pod = load_pod()
    if pod is None:
        print(f"ERROR: No saved pod found at {POD_STATE_PATH} -- start one first.")
        return
    extract_pdf_images(skip_done=False)
    generate_text(pod.pod_id, RUNPOD_API_KEY)
    """
    generate_text(pod.pod_id, RUNPOD_API_KEY)
    print("INFO: pod left open, info saved in txt file")
    """
    
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