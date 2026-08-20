# controller.py

import subprocess
import csv
import json
import time
import requests
from enum import Enum, auto
from pathlib import Path

# --- config ---
RUNPOD_ENDPOINT = "..."      # your pod's exposed URL
MODEL_NAME = "..."           # fixed, single model
INPUT_DIR = "./PDFTesting"
JAR_PATH = "./pdffigures2/pdffigures2.jar"
EXTRACT_OUT_DIR = "./pdffigures2_out"
RESULTS_PATH = "./alt_text_results.json"   # or .csv — decide below
RUNPOD_API_KEY = "" #TODO: change to os environ before deploying

#Checks if the required services and files have been created in order to run pdf_batch_runner.py
def configChecks():
    #check if colima is running
    result = subprocess.run(
        ["colima", "status"],
         capture_output=True,
         text=True,
    )
    
    if (result.returncode == 1):
        print("Colima is not running. Please start Colima and try again.")
        return False  
    
    #checks if the pdffigures2.jar file exists
    result = subprocess.run(
        ["ls", " pdffigures2/pdffigures2.jar"],
            capture_output=True,
            text=True,
    )
    
    if (result.returncode == 0):
        print("pdffigures2.0 jar file has not been created, check step 3 in SETUP.md ")
        return False
    
    return True
    

def run_extraction():
    if not configChecks():
        return False
    
    PROJECT_ROOT = Path(__file__).resolve().parent
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{PROJECT_ROOT}:/work",
        "-w", "/work",
        "--entrypoint", "bash",
        "pdffigures2-builder",
        "-c", "python3 pdf_batch_runner.py --input-dir ./PDFTesting --jar ./pdffigures2/pdffigures2.jar --output-dir ./pdffigures2_out --dpi 300 --java-heap 6g -v",
    ]        
    result = subprocess.run(
        cmd,
        capture_output = True,
        text = True,
    )                                                         
    
    print(result.stderr)   
    if result.returncode != 0:
        print("EXTRACTION FAILED. SEE LOGS ABVOVE")
        return False

    return True    

    """Shell out to pdf_batch_runner.py. Return True/False on success."""
    ...

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
    print(run_extraction())

def hi():
    if not run_extraction():
        # decide: abort, or continue with stale manifest?
        ...
    manifest_rows = load_manifest()
    existing = load_existing_results()

    for row in manifest_rows:
        if row["image_path"] in existing:
            continue
        try:
            result = call_with_retry(row)
            save_result(row, result)
        except Exception as e:
            # log and move on — one bad image shouldn't kill the run
            ...

import time
import requests
from enum import Enum, auto


class podStatus(Enum):
    TERMINATED = auto()      # pod does not exist anymore
    STARTING = auto()
    READY = auto()           # pod is up, network-reachable, idle
    RUNNING_MODEL = auto()   # pod is up and actively serving an inference request
    EXITED = auto()          # pod exists but stopped — needs restart
    UNKNOWN = auto()         # error during the pod status check


class Pod:
    def __init__(self, pod_name, pod_status: podStatus, pod_id, port):
        if not isinstance(pod_status, podStatus):
            raise TypeError(f"pod_status must be a podStatus, got {type(pod_status)}")

        self.pod_name = pod_name
        self.pod_status = pod_status
        self.pod_id = pod_id
        self.port = port

    #<----Pod status check functions----->
    def __get_pod_status(self, api_key):
        url = f"https://rest.runpod.io/v1/pods/{self.pod_id}"  # bug fix: was `pod_id`
        headers = {"Authorization": f"Bearer {api_key}"}
        r = requests.get(url, headers=headers)
        r.raise_for_status()
        return r.json()

    def __is_pod_network_ready(self, api_key):
        """RunPod infra-level check only — knows nothing about your model process."""
        pod = self.__get_pod_status(api_key)
        status_ok = pod["desiredStatus"] == "RUNNING"
        network_ready = bool(pod.get("publicIp")) and bool(pod.get("portMappings"))
        return status_ok and network_ready, pod

    def __is_model_busy(self) -> bool:
        """Asks YOUR inference server's /status route — the only source of truth
        for RUNNING_MODEL vs READY, since RunPod's API has no visibility into this."""
        try:
            r = requests.get(f"http://{self.public_ip}:{self.port}/status", timeout=5)
            r.raise_for_status()
            return r.json()["busy"]
        except requests.RequestException:
            # server not reachable yet / not up — treat as not busy, caller
            # should already have confirmed network_ready before calling this
            return False

    def app_startup_pod_checker(self, api_key) -> podStatus:
        try:
            network_ready, pod = self.__is_pod_network_ready(api_key)
        except requests.RequestException:
            self.pod_status = podStatus.UNKNOWN
            return self.pod_status

        status = pod.get("desiredStatus")

        if status == "TERMINATED":
            self.pod_status = podStatus.TERMINATED
        elif status == "EXITED":
            self.pod_status = podStatus.EXITED
        elif status == "RUNNING":
            if not network_ready:
                self.pod_status = podStatus.STARTING
            else:
                self.public_ip = pod["publicIp"]
                self.pod_status = (
                    podStatus.RUNNING_MODEL if self.__is_model_busy() else podStatus.READY
                )
        else:
            self.pod_status = podStatus.UNKNOWN

        return self.pod_status
    
    """
    For the startup/recovery phase
    Should be called after creating a pod
    Should be called after restarting a exited pod
    Should be called when pod status returns UNKNOWN, better then hammering API in a customn loop
    DO NOT CALL BEFORE EVERY INFERENCE REQUEST. Call app_startup_pod_checker instead
    DO NOT CALL ON TERMINATED POD. Will throw timeout error
    """
    def wait_for_pod(self, api_key, timeout_s=180, interval_s=5):
        start = time.time()
        while time.time() - start < timeout_s:
            state = self.app_startup_pod_checker(api_key)
            if state in (podStatus.READY, podStatus.RUNNING_MODEL):
                return state
            time.sleep(interval_s)
        raise TimeoutError(f"Pod {self.pod_id} not ready after {timeout_s}s")



if __name__ == "__main__":
    main()