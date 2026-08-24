"""
job_pipeline.py

Per-job orchestration: extraction -> shared GPU pod -> alt-text generation.
Called by worker.py's main loop, one job at a time (matches the confirmed
sequential, one-PDF-at-a-time processing model).

Pod lifecycle: at <=10 PDFs/day, cold-starting a fresh pod for every job
would spend most of its time on the 5-7 minute boot rather than actual work.
Instead one pod is shared across jobs and kept alive for up to
POD_MAX_LIFETIME_S (~4h), reused by ensure_pod_ready() for each new job that
arrives within that window, and only recreated once it expires or is found
dead. It's retired lazily (next job that needs one notices the TTL) or
proactively by the worker's idle-tick sweep -- see retire_expired_pod_if_any().
"""

import logging
import time

import controller
import job_store
import telegram_status
from pod import Pod, podStatus
from pdf_batch_runner import extract_images
from runpod_VL import generate_alt_text_for_manifest

POD_MAX_LIFETIME_S = 4 * 60 * 60  # ~4h, per confirmed low volume (<=10 PDFs/day) --
                                   # reusing one pod across a session's jobs avoids
                                   # repeated multi-minute cold starts for a handful of jobs/day


def retire_pod(pod: Pod | None, api_key: str) -> None:
    """No-op if pod is None. Termination failure is a billing risk, not just
    a log line -- it gets a dedicated Telegram alert. On success (including
    the pod already being gone -- terminate_pod() is idempotent on 404),
    clears the saved pod state so the next check doesn't keep retrying
    against the same already-retired pod_id."""
    if pod is None:
        return
    try:
        controller.terminate_pod(pod, api_key)
        controller.clear_pod()
    except Exception:
        logging.exception("failed to terminate expired/dead pod %s", pod.pod_id)
        telegram_status.alert_orphan_pod(pod.pod_id, job_id=None)


def expired_pod_or_none(api_key: str) -> Pod | None:
    """Returns the saved pod only if it's past POD_MAX_LIFETIME_S -- used by
    worker.py's idle loop to proactively retire a pod nothing is using
    anymore, rather than letting it sit billed until some future job happens
    to ask for one."""
    pod = controller.load_pod()
    if pod is None or pod.created_at is None:
        return None
    if (time.time() - pod.created_at) >= POD_MAX_LIFETIME_S:
        return pod
    return None


def ensure_pod_ready(api_key: str) -> Pod:
    """Reuses the saved pod if it's still within its lifetime and actually
    healthy; otherwise retires it (if any) and starts a fresh one."""
    pod = controller.load_pod()
    if pod is not None and pod.created_at is not None and (time.time() - pod.created_at) < POD_MAX_LIFETIME_S:
        status = pod.app_startup_pod_checker(api_key)  # refresh -- don't trust cached state,
        if status in (podStatus.READY, podStatus.RUNNING_MODEL):  # RunPod could've evicted it
            return pod

    retire_pod(pod, api_key)
    pod = controller.start_new_pod(api_key)
    controller.save_pod(pod)
    return pod


def process_job(job: job_store.Job, api_key: str) -> None:
    """Runs one job through the full pipeline. job is assumed to already be
    in EXTRACTING state (job_store.claim_next_queued_job() sets that
    atomically) -- this does NOT re-set it. Never raises: failures are
    recorded on the job itself so the worker loop can move on to the next one."""
    try:
        telegram_status.refresh()  # reflect the EXTRACTING state claim_next_queued_job() already set
        extract_images(input_dir=job.pdf_dir, output_dir=job.extract_dir, skip_done=False)
        job_store.update_job_state(job.job_id, "EXTRACTED")
        telegram_status.refresh()

        job_store.update_job_state(job.job_id, "POD_STARTING")
        telegram_status.refresh()
        pod = ensure_pod_ready(api_key)  # usually fast: reuses the already-warm pod
        job_store.set_pod_id(job.job_id, pod.pod_id)

        job_store.update_job_state(job.job_id, "GENERATING")
        telegram_status.refresh()
        generate_alt_text_for_manifest(
            pod.pod_id, api_key,
            manifest_path=job.manifest_path,
            output_csv=job.results_csv_path,
        )

        job_store.update_job_state(job.job_id, "COMPLETE")
    except Exception as exc:
        job_store.update_job_state(job.job_id, "FAILED", error=str(exc))
        logging.exception("job %s failed", job.job_id)
    finally:
        telegram_status.refresh()
