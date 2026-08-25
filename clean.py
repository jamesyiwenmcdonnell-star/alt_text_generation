"""
clean.py

DESTRUCTIVE, IRREVERSIBLE: permanently deletes every jobs/<job_id>/ directory,
resets jobs.db to empty, and clears the Telegram status board (deletes every
message this bot has tracked sending, via telegram_status.startup_cleanup()).
For a full reset -- e.g. wiping history that's no longer needed, or starting
over during development.

Requires typing an exact confirmation phrase; nothing is deleted otherwise.

Run this with the daemon STOPPED first (docker stop alttext-pipeline) --
running it while worker.py is actively mid-job risks deleting a job's files
or DB row out from under it while it's still being written. This script
warns (but does not refuse) if it finds jobs in a non-terminal state.

Usage (needs an interactive terminal for the confirmation prompt):
    docker exec -it alttext-pipeline python3 clean.py
"""

import logging
import os
import shutil
import sys

import job_store
import telegram_status

CONFIRM_PHRASE = "DELETE ALL"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    job_store.init_db()  # in case jobs.db doesn't exist yet -- nothing to delete either way

    active_states = ("QUEUED",) + job_store.IN_PROGRESS_STATES
    active_jobs = job_store.list_jobs(state=active_states, limit=1000)
    total_jobs = job_store.count_jobs()

    print("This will permanently delete:")
    print(f"  - {total_jobs} job record(s) and their files under jobs/")
    print("  - the Telegram status board and every message this bot has tracked sending")
    print()

    if active_jobs:
        print(f"WARNING: {len(active_jobs)} job(s) are NOT in a terminal state "
              f"(QUEUED/EXTRACTING/EXTRACTED/POD_STARTING/GENERATING):")
        for j in active_jobs:
            print(f"    {j.job_id}  ({j.state})")
        print("If the worker is still running, deleting now can corrupt an in-flight job.")
        print("Stop the daemon first: docker stop alttext-pipeline")
        print()

    print(f"Type '{CONFIRM_PHRASE}' to proceed, anything else to abort.")
    answer = input("> ").strip()
    if answer != CONFIRM_PHRASE:
        print("Aborted -- nothing was deleted.")
        return 1

    # -- job data --
    if os.path.isdir(job_store.JOBS_ROOT):
        shutil.rmtree(job_store.JOBS_ROOT)
        os.makedirs(job_store.JOBS_ROOT, exist_ok=True)
    for db_file in (job_store.DB_PATH, job_store.DB_PATH + "-wal", job_store.DB_PATH + "-shm"):
        if os.path.exists(db_file):
            os.remove(db_file)
    job_store.init_db()
    print(f"Deleted all job data ({total_jobs} job(s)) and reset jobs.db.")

    # -- telegram --
    telegram_status.startup_cleanup()
    print("Cleared the Telegram status board.")

    print("Clean complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
