"""
worker.py

Long-running daemon: claims one QUEUED job at a time from job_store and
drives it through job_pipeline.process_job(). Runs alongside api_server.py
as a second foreground process in the same container (see entrypoint.sh) --
kept as a separate OS process rather than a background task inside the API's
event loop, since extraction/pod-wait/generation are long synchronous calls
that would otherwise starve status requests.

Usage:
    python3 worker.py
"""

import logging
import os
import sys
import time

import controller
import job_pipeline
import job_store
import telegram_status

POLL_INTERVAL_S = 10
REQUIRED_ENV_VARS = ("RUNPOD_API_KEY", "HF_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


def validate_required_env() -> None:
    missing = [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]
    if missing:
        print(f"worker.py: missing required environment variable(s): {', '.join(missing)}", file=sys.stderr)
        raise SystemExit(1)


def recover_from_crash(api_key: str) -> None:
    """Runs once at startup. Pod-per-session means there should never be more
    than one live pod -- anything found alive now is leftover from a crash,
    so sweep it. Jobs left mid-pipeline are marked FAILED rather than
    silently resumed, since partial extraction output on disk may be stale;
    a fresh resubmission is safer than guessing where a crashed job left off."""
    controller.terminate_all_pods(api_key)
    for stuck in job_store.list_jobs(state=job_store.IN_PROGRESS_STATES, limit=1000):
        job_store.update_job_state(stuck.job_id, "FAILED", error="worker restarted mid-job")
        logging.warning("marked stuck job %s FAILED (was %s)", stuck.job_id, stuck.state)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    validate_required_env()

    api_key = os.environ["RUNPOD_API_KEY"]

    job_store.init_db()
    recover_from_crash(api_key)
    # Deliberately non-fatal, as a block: the status board is cosmetic, and no
    # Telegram failure -- outage, revoked token, quote-wrapped credentials --
    # is a reason to refuse to process jobs. Unguarded, these three lines put
    # the container in a permanent restart loop and took the intake API down
    # with them (see telegram_status.refresh_safely()).
    try:
        telegram_status.startup_cleanup()  # wipe messages from previous runs before posting a fresh board
        telegram_status.ensure_message()
        telegram_status.refresh()
    except Exception:
        logging.exception("telegram status board unavailable at startup -- "
                          "continuing without it")

    logging.info("worker started, polling every %ds", POLL_INTERVAL_S)
    while True:
        job = job_store.claim_next_queued_job()
        if job is None:
            job_pipeline.retire_pod(job_pipeline.expired_pod_or_none(api_key), api_key)
            job_pipeline.cleanup_old_failed_job_files()
            time.sleep(POLL_INTERVAL_S)
            continue

        logging.info("processing job %s (%s, editor=%s)", job.job_id, job.pdf_filename, job.editor_id)
        job_pipeline.process_job(job, api_key)


if __name__ == "__main__":
    main()
