import os
import requests
from pathlib import Path
from gpu_utils import pick_available_gpu
from pod import Pod, podStatus, list_pods, terminate_pod_by_id

# --- config ---
POD_STATE_PATH = "./pod_state.txt"
RUNPOD_API_KEY = os.environ.get("RUNPOD_API_KEY", "")
HF_TOKEN = os.environ.get("HF_TOKEN", "")

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
    (e.g. after this process crashed or was interrupted, or a future job
    reusing a still-warm pod) can recover it without needing to know its id
    ahead of time."""
    lines = [
        f"pod_name={pod.pod_name}",
        f"pod_id={pod.pod_id}",
        f"port={pod.port}",
        f"pod_status={pod.pod_status.name}",
        f"created_at={pod.created_at}",
    ]
    Path(path).write_text("\n".join(lines) + "\n")


def clear_pod(path: str = POD_STATE_PATH) -> None:
    """Removes the saved pod state file, e.g. after the pod it describes has
    been terminated -- so load_pod() correctly returns None afterward instead
    of repeatedly returning a stale, already-gone pod on every future call."""
    Path(path).unlink(missing_ok=True)


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
    created_at = _parse_optional(fields.get("created_at"))
    return Pod(
        pod_name=fields.get("pod_name"),
        pod_status=podStatus[fields["pod_status"]],
        pod_id=_parse_optional(fields.get("pod_id")),
        port=int(port) if port is not None else None,
        created_at=float(created_at) if created_at is not None else None,
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
