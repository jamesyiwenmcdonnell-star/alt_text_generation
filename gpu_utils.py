"""
These functions are for managing RUNPOD's GPU availability
At times, the NIVIDIA A40 (our main GPU) will be unavailable, so compatible alternative GPUs will need to be used.
pick_available_GPU() function handles cases where the A40 is unavailable
Or when GPU ids have changed and need to be updated within the gpu_ids_snapshot.txt file
"""

import requests
import json
from pathlib import Path

def check_for_id_drift(api_key: str, snapshot_path: str = "gpu_ids_snapshot.txt"):
    query = "query { gpuTypes { id displayName } }"
    response = requests.post(
        "https://api.runpod.io/graphql",
        params={"api_key": api_key},
        json={"query": query},
    )
    response.raise_for_status()
    current_ids = {g["id"] for g in response.json()["data"]["gpuTypes"]}

    snapshot_file = Path(snapshot_path)

    if not snapshot_file.exists():
        snapshot_file.write_text("\n".join(sorted(current_ids)) + "\n")
        print("No prior snapshot — baseline saved.")
        return

    previous_ids = set(snapshot_file.read_text().splitlines())

    added = current_ids - previous_ids
    removed = previous_ids - current_ids

    if added:
        print(f"New GPU IDs appeared: {sorted(added)}")
    if removed:
        print(f"GPU IDs disappeared (possibly renamed or delisted): {sorted(removed)}")

    if added or removed:
        snapshot_file.write_text("\n".join(sorted(current_ids)) + "\n")

#Returns a list of the GPU ids
def extract_gpu_ids(snapshot_path: str = "gpu_ids_snapshot.txt") -> list[str]:
    snapshot_file = Path(snapshot_path)

    if not snapshot_file.exists():
        raise FileNotFoundError(
            f"{snapshot_path} does not exist — run check_for_id_drift() first"
        )

    with snapshot_file.open("r") as f:
        gpu_ids = [line.strip() for line in f if line.strip()]

    return gpu_ids

def get_gpu_stock_status(gpu_type_id: str, api_key: str, gpu_count: int = 1) -> dict | None:
    query = """
    query($id: String!, $gpuCount: Int!) {
      gpuTypes(input: { id: $id }) {
        id
        displayName
        lowestPrice(input: { gpuCount: $gpuCount, secureCloud: true }) {
          stockStatus
          uninterruptablePrice
          availableGpuCounts
        }
      }
    }
    """
    response = requests.post(
        "https://api.runpod.io/graphql",
        params={"api_key": api_key},
        json={"query": query, "variables": {"id": gpu_type_id, "gpuCount": gpu_count}},
    )
    response.raise_for_status()
    body = response.json()

    if "errors" in body:
        print(f"GraphQL error for {gpu_type_id!r}: {body['errors']}")
        return None

    gpu_types = body["data"]["gpuTypes"]
    if not gpu_types:
        print(f"No GPU type found matching id={gpu_type_id!r} — check the exact ID string")
        return None

    return gpu_types[0]["lowestPrice"]

def check_gpu_id(gpu_type_id: str, api_key: str) -> str:
    """Returns one of: 'valid', 'invalid_id', 'query_error'."""
    query = "query($id: String!) { gpuTypes(input: { id: $id }) { id displayName } }"
    response = requests.post(
        "https://api.runpod.io/graphql",
        params={"api_key": api_key},
        json={"query": query, "variables": {"id": gpu_type_id}},
    )
    response.raise_for_status()
    body = response.json()

    if "errors" in body:
        return "query_error"
    if not body["data"]["gpuTypes"]:
        return "invalid_id"
    return "valid"

def list_all_gpu_ids(api_key: str, out_path: str = "gpu_types.json"):
    query = "query { gpuTypes { id displayName } }"
    response = requests.post(
        "https://api.runpod.io/graphql",
        params={"api_key": api_key},
        json={"query": query},
    )
    response.raise_for_status()
    body = response.json()

    if "errors" in body:
        print("GraphQL errors:", body["errors"])
        return

    gpu_types = body["data"]["gpuTypes"]
    print(f"Total GPU types returned: {len(gpu_types)}")

    with open(out_path, "w") as f:
        json.dump(gpu_types, f, indent=2)
    print(f"Full list written to {out_path}")

    # Explicitly confirm the ones you actually care about are present
    for target in ["H100", "A100", "A40", "L40", "RTX A6000", "RTX 6000 Ada"]:
        matches = [g for g in gpu_types if target in g["id"]]
        print(f"Matches containing {target!r}: {matches}")

"""
Function checks if GPU id is valid before running availability checks
To protect the project from GPU id changes breaking the pipeline
If a invalid GPU id is detected, will use the check_for_id_drift() function once per call 
returns the id of the GPU that is available
"""
def pick_available_gpu(api_key: str) -> str | None:
    any_invalid = False
    gpu_candidates = extract_gpu_ids()                                  #Extracting GPU Ids from gpu_ids_snapshot.txt

    for gpu_id in gpu_candidates:
        status = check_gpu_id(gpu_id, api_key)
        if status == "invalid_id":
            print(f"WARNING: {gpu_id!r} is not a recognized GPU type")
            any_invalid = True
            continue
        if status == "query_error":
            print(f"Query error checking {gpu_id!r} — skipping")
            continue

        stock = get_gpu_stock_status(gpu_id, api_key)
        if stock and stock["stockStatus"] != "Low":
            return gpu_id

    if any_invalid:
        check_for_id_drift(api_key)                                     # runs once per call, not once per candidate

    return None


