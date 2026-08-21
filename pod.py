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
    def __init__(self, pod_name, pod_status: podStatus, pod_id=None, port=None):
        if not isinstance(pod_status, podStatus):
            raise TypeError(f"pod_status must be a podStatus, got {type(pod_status)}")

        self.pod_name = pod_name
        self.pod_status = pod_status
        self.pod_id = pod_id
        self.port = port

    # <----Pod start/stop functions----->

    def start_pod(self, payload: dict, api_key: str) -> dict:
        """Creates a new pod via REST API. payload must already include gpuTypeIds."""
        url = "https://rest.runpod.io/v1/pods"
        headers = {
            "Authorization": f"Bearer {api_key}",  # bug fix: was referencing global RUNPOD_API_KEY, not the api_key param
            "Content-Type": "application/json",
        }
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        pod_data = response.json()

        self.pod_id = pod_data["id"]
        self.port = 8000  # matches RUNPOD_POD_CONFIG's exposed port
        self.pod_status = podStatus.STARTING
        return pod_data

    # <----Pod status check functions----->

    def __get_pod_status(self, api_key):
        url = f"https://rest.runpod.io/v1/pods/{self.pod_id}"
        headers = {"Authorization": f"Bearer {api_key}"}
        r = requests.get(url, headers=headers)
        r.raise_for_status()
        return r.json()

    def __is_pod_network_ready(self, api_key):
        pod = self.__get_pod_status(api_key)
        status_ok = pod["desiredStatus"] == "RUNNING"
        network_ready = bool(pod.get("publicIp")) and bool(pod.get("portMappings"))
        return status_ok and network_ready, pod

    def __is_model_busy(self) -> bool:
        try:
            r = requests.get(f"http://{self.public_ip}:{self.port}/status", timeout=5)
            r.raise_for_status()
            return r.json()["busy"]
        except requests.RequestException:
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

        print(str(self.pod_status))
        return self.pod_status

    def wait_for_pod(self, api_key, timeout_s=180, interval_s=5):
        start = time.time()
        while time.time() - start < timeout_s:
            state = self.app_startup_pod_checker(api_key)
            if state in (podStatus.READY, podStatus.RUNNING_MODEL):
                return state
            time.sleep(interval_s)
        raise TimeoutError(f"Pod {self.pod_id} not ready after {timeout_s}s")