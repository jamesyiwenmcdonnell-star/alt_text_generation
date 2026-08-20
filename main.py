# controller.py

import subprocess
import csv
import json
import time
import requests
from pathlib import Path

# --- config ---
RUNPOD_ENDPOINT = "..."      # your pod's exposed URL
MODEL_NAME = "..."           # fixed, single model
INPUT_DIR = "./PDFTesting"
JAR_PATH = "./pdffigures2/pdffigures2.jar"
EXTRACT_OUT_DIR = "./pdffigures2_out"
RESULTS_PATH = "./alt_text_results.json"   # or .csv — decide below

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

if __name__ == "__main__":
    main()