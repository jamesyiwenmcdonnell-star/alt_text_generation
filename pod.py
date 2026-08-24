import time
import requests
from enum import Enum, auto

REQUEST_TIMEOUT = 30  # seconds -- without this, a slow/unreachable RunPod API call hangs silently forever

class podStatus(Enum):
    TERMINATED = auto()      # pod does not exist anymore
    STARTING = auto()
    READY = auto()           # pod is up, network-reachable, idle
    RUNNING_MODEL = auto()   # pod is up and actively serving an inference request
    EXITED = auto()          # pod exists but stopped — needs restart
    UNKNOWN = auto()         # error during the pod status check


def list_pods(api_key: str) -> list[dict]:
    """Returns every pod on the account via REST API, regardless of status."""
    url = "https://rest.runpod.io/v1/pods"
    headers = {"Authorization": f"Bearer {api_key}"}
    r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


def terminate_pod_by_id(pod_id: str, api_key: str) -> None:
    """Permanently deletes a pod by id -- doesn't require a live Pod instance."""
    url = f"https://rest.runpod.io/v1/pods/{pod_id}"
    headers = {"Authorization": f"Bearer {api_key}"}
    response = requests.delete(url, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()  # 204 No Content on success -- no body to parse


class Pod:
    def __init__(self, pod_name, pod_status: podStatus, pod_id=None, port=None, created_at=None):
        if not isinstance(pod_status, podStatus):
            raise TypeError(f"pod_status must be a podStatus, got {type(pod_status)}")

        self.pod_name = pod_name
        self.pod_status = pod_status
        self.pod_id = pod_id
        self.port = port
        self.created_at = created_at  # unix timestamp; set for real in start_pod(),
                                       # or restored from saved state by the caller

    # <----Pod start/stop functions----->

    def start_pod(self, payload: dict, api_key: str) -> dict:
        """Creates a new pod via REST API. payload must already include gpuTypeIds."""
        url = "https://rest.runpod.io/v1/pods"
        headers = {
            "Authorization": f"Bearer {api_key}",  # bug fix: was referencing global RUNPOD_API_KEY, not the api_key param
            "Content-Type": "application/json",
        }
        response = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        pod_data = response.json()

        self.pod_id = pod_data["id"]
        self.port = 8000  # matches RUNPOD_POD_CONFIG's exposed port
        self.pod_status = podStatus.STARTING
        self.created_at = time.time()
        return pod_data

    def stop_pod(self, api_key: str) -> dict:
        """Stops the pod via REST API. Pod and its storage are preserved --
        it can be restarted later, unlike terminate_pod."""
        url = f"https://rest.runpod.io/v1/pods/{self.pod_id}/stop"
        headers = {"Authorization": f"Bearer {api_key}"}
        response = requests.post(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        self.pod_status = podStatus.EXITED
        return response.json()

    def terminate_pod(self, api_key: str) -> None:
        """Permanently deletes the pod via REST API. Unlike stop_pod, this is
        not reversible -- the pod and its storage are gone. Idempotent: a 404
        means the pod is already gone (e.g. a stale saved pod_id, or already
        cleaned up), which is the outcome this method wants anyway -- treat
        it as success rather than raising, so callers don't have to special-
        case "already gone" vs. "termination genuinely failed"."""
        url = f"https://rest.runpod.io/v1/pods/{self.pod_id}"
        headers = {"Authorization": f"Bearer {api_key}"}
        response = requests.delete(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if response.status_code != 404:
            response.raise_for_status()  # 204 No Content on success -- no body to parse
        self.pod_status = podStatus.TERMINATED

    # <----Pod status check functions----->

    def __get_pod_status(self, api_key):
        url = f"https://rest.runpod.io/v1/pods/{self.pod_id}"
        headers = {"Authorization": f"Bearer {api_key}"}
        r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()

    def __is_pod_network_ready(self, api_key):
        """publicIp/portMappings only ever populate for direct TCP port
        exposure (SSH, databases, ...). Our pod exposes 8000/http, an
        HTTP-type port -- those are only reachable via RunPod's HTTPS proxy
        (proxy_url()) and never get a publicIp assigned, so desiredStatus is
        the only signal available here."""
        pod = self.__get_pod_status(api_key)
        return pod["desiredStatus"] == "RUNNING", pod

    def proxy_url(self) -> str:
        return f"https://{self.pod_id}-{self.port}.proxy.runpod.net"

    def __is_model_serving(self) -> bool:
        """vLLM's OpenAI server exposes /health once the engine has finished
        loading -- there's no documented busy/idle signal, so this only
        confirms the server is up and answering, not whether it's idle."""
        try:
            r = requests.get(f"{self.proxy_url()}/health", timeout=5)
            return r.status_code == 200
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
                # pod infra is up, but vLLM may still be downloading/loading
                # the model -- stay STARTING until /health actually answers
                self.pod_status = podStatus.READY if self.__is_model_serving() else podStatus.STARTING
        else:
            self.pod_status = podStatus.UNKNOWN

        return self.pod_status

    @staticmethod
    def _print_progress(fraction: float, label: str, width: int = 30) -> None:
        fraction = min(max(fraction, 0.0), 1.0)
        filled = int(width * fraction)
        bar = "#" * filled + "-" * (width - filled)
        print(f"\r[{bar}] {int(fraction * 100):3d}% {label:<20}", end="", flush=True)

    def wait_for_pod(self, api_key, timeout_s=420, interval_s=5):
        start = time.time()
        while True:
            state = self.app_startup_pod_checker(api_key)
            elapsed = time.time() - start

            if state in (podStatus.READY, podStatus.RUNNING_MODEL):
                self._print_progress(1.0, str(state))
                print()
                return state
            if state in (podStatus.TERMINATED, podStatus.EXITED):
                print()
                raise RuntimeError(f"Pod {self.pod_id} entered {state} while waiting for it to become ready")

            self._print_progress(elapsed / timeout_s, str(state))

            if elapsed >= timeout_s:
                print()
                raise TimeoutError(f"Pod {self.pod_id} not ready after {timeout_s}s")

            time.sleep(interval_s)