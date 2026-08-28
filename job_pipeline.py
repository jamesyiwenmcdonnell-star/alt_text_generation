"""
job_pipeline.py

Per-job orchestration: extraction -> shared GPU pod -> alt-text generation ->
embedding. Called by worker.py's main loop, one job at a time (matches the
confirmed sequential, one-PDF-at-a-time processing model).

Pod lifecycle: at <=10 PDFs/day, cold-starting a fresh pod for every job
would spend most of its time on the 5-7 minute boot rather than actual work.
Instead one pod is shared across jobs and kept alive for up to
POD_MAX_LIFETIME_S (~4h), reused by ensure_pod_ready() for each new job that
arrives within that window, and only recreated once it expires or is found
dead. It's retired lazily (next job that needs one notices the TTL) or
proactively by the worker's idle-tick sweep -- see retire_expired_pod_if_any().
"""

import logging
import os
import shutil
import time
from datetime import datetime, timedelta, timezone

import controller
import embed_alt_text
import job_store
import telegram_status
from pod import Pod, podStatus
from pdf_batch_runner import extract_images
from runpod_VL import generate_alt_text_for_manifest

POD_MAX_LIFETIME_S = 60 * 60  # ~1h, per confirmed low volume (<=10 PDFs/day) --
                                   # reusing one pod across a session's jobs avoids
                                   # repeated multi-minute cold starts for a handful of jobs/day

# A FAILED job's on-disk files (PDF, any partial extraction output) get
# deleted after this long. The DB row itself is untouched -- it stays
# queryable via the API and keeps showing on the Telegram board until ITS OWN,
# longer window elapses (see telegram_status.py's FAILED_DISPLAY_WORKING_DAYS).
# COMPLETE jobs' files are never deleted, by design -- no equivalent constant.
FAILED_JOB_FILE_RETENTION_HOURS = 24


def cleanup_old_failed_job_files() -> None:
    """Deletes jobs/<job_id>/ for any FAILED job whose completed_at is older
    than FAILED_JOB_FILE_RETENTION_HOURS. Safe to call repeatedly -- once a
    job's directory is gone, later calls just skip it."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=FAILED_JOB_FILE_RETENTION_HOURS))
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    for job in job_store.list_jobs_completed_before("FAILED", cutoff_iso):
        if os.path.isdir(job.job_dir):
            shutil.rmtree(job.job_dir)
            logging.info("deleted on-disk files for expired FAILED job %s", job.job_id)


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
        try:
            telegram_status.alert_orphan_pod(pod.pod_id, job_id=None)
        except Exception:
            # The pod leak is already logged above, and it is the thing that
            # matters. Failing to *announce* it must not additionally unwind
            # worker.py's idle loop and kill the daemon.
            logging.exception("could not send orphan-pod alert for %s", pod.pod_id)


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


# Below this fraction of manifest figures tagged, a job is flagged as
# under-embedded -- both by the pre-generation confidence check and by the
# post-embedding coverage check. Documents whose structure the embedder
# actually understands sit at 99-100% on the test corpus; anything materially
# below that means whole figures will be silent for a screen-reader user, and
# the editor needs to know rather than receiving a tagged PDF that *looks*
# complete. Deliberately non-fatal either way: partial tagging plus a warning
# beats discarding good alt text.
EMBED_MIN_COVERAGE = 0.90


def _coverage_note(tagged: int, total: int, coverage: float,
                   verb: str, ok_prefix: str) -> tuple[str, str, bool]:
    """Single source of the embed-confidence note for both the precheck and
    the post-embed check, so the threshold comparison and the
    job_store.LOW_CONFIDENCE_PREFIX marker that telegram_status and
    api_server key on can't drift apart. Returns (text, note, low)."""
    text = f"{tagged}/{total} figures {verb} ({coverage:.0%})"
    low = coverage < EMBED_MIN_COVERAGE
    note = (f"{job_store.LOW_CONFIDENCE_PREFIX}: only {text}" if low
            else f"{ok_prefix}: {text}")
    return text, note, low


def precheck_embedding(job: job_store.Job) -> None:
    """Pre-generation confidence check: predicts embedding coverage by running
    the real matching logic in dry-run mode (no alt text needed -- matching is
    purely geometric), BEFORE any GPU time is spent. The verdict is persisted
    on the job (embed_precheck_coverage / embed_note) so an under-embeddable
    PDF is visible on the board while there's still time to cancel it.

    Never raises -- a broken precheck must not take down a job that might
    still generate perfectly good alt text. SystemExit is included for the
    same reason as in embed_alt_text_into_pdf below."""
    try:
        pre = embed_alt_text.preflight(job.pdf_path, job.manifest_path, job.pdf_stem)
        if not pre["ok"]:
            note = f"{job_store.LOW_CONFIDENCE_PREFIX}: cannot embed -- {pre['reason']}"
            coverage = 0.0
        else:
            coverage = pre["coverage"]
            _text, note, _low = _coverage_note(pre["tagged"], pre["total"], coverage,
                                               verb="matchable", ok_prefix="embed precheck")
        job_store.set_embed_precheck(job.job_id, coverage, note)
        logging.info("job %s: %s", job.job_id, note)
    except (Exception, SystemExit):
        logging.exception("job %s: embed precheck itself failed (continuing)", job.job_id)


def embed_alt_text_into_pdf(job: job_store.Job) -> dict:
    """Splices this job's generated alt text into a tagged copy of its source
    PDF at job.tagged_pdf_path, and returns embed_alt_text.summarize_report()'s
    summary dict (total / tagged / coverage / ...).

    embed_alt_text.embed() signals its own failure paths -- no /StructTreeRoot
    to splice into, no manifest rows for this pdf_name, an empty /K -- by
    raising SystemExit, which is correct for a standalone CLI but is a
    BaseException, so process_job()'s `except Exception` would not stop it.
    Left alone it would unwind all the way out through worker.py's polling
    loop and kill the daemon. Converting it here rather than in
    embed_alt_text.py keeps that script's CLI behaviour intact.
    """
    try:
        report = embed_alt_text.embed(
            pdf_path=job.pdf_path,
            manifest_path=job.manifest_path,
            alt_paths=[job.results_csv_path],
            out_path=job.tagged_pdf_path,
            pdf_name=job.pdf_stem,
        )
    except SystemExit as exc:
        raise RuntimeError(str(exc) or "embed_alt_text aborted") from exc

    return embed_alt_text.summarize_report(report)


def process_job(job: job_store.Job, api_key: str) -> None:
    """Runs one job through the full pipeline. job is assumed to already be
    in EXTRACTING state (job_store.claim_next_queued_job() sets that
    atomically) -- this does NOT re-set it. Never raises: failures are
    recorded on the job itself so the worker loop can move on to the next one."""
    try:
        telegram_status.refresh_safely()  # reflect the EXTRACTING state claim_next_queued_job() already set
        extract_images(input_dir=job.pdf_dir, output_dir=job.extract_dir, skip_done=False)
        job_store.update_job_state(job.job_id, "EXTRACTED")
        precheck_embedding(job)  # before the pod: flags un-embeddable PDFs while no GPU money is being spent
        telegram_status.refresh_safely()

        job_store.update_job_state(job.job_id, "POD_STARTING")
        telegram_status.refresh_safely()
        pod = ensure_pod_ready(api_key)  # usually fast: reuses the already-warm pod
        job_store.set_pod_id(job.job_id, pod.pod_id)

        job_store.update_job_state(job.job_id, "GENERATING")
        telegram_status.refresh_safely()
        generate_alt_text_for_manifest(
            pod.pod_id, api_key,
            manifest_path=job.manifest_path,
            output_csv=job.results_csv_path,
        )

        job_store.update_job_state(job.job_id, "EMBEDDING")
        telegram_status.refresh_safely()
        embed_warning = None
        try:
            summary = embed_alt_text_into_pdf(job)
            coverage = summary["coverage"]
            text, note, low = _coverage_note(summary["tagged"], summary["total"], coverage,
                                             verb="tagged", ok_prefix="embedded")
            if low:
                # rides on error_message too so the COMPLETE state carries the
                # caveat the same way an embed crash does (⚠ on the board,
                # visible in the API) -- an under-tagged PDF must not look
                # identical to a fully tagged one.
                embed_warning = f"embedded, but only {text} -- tagged PDF is incomplete"
            job_store.set_embed_result(job.job_id, coverage, note)
            logging.info("job %s: %s -> %s", job.job_id, note, job.tagged_pdf_path)
        except (Exception, SystemExit) as exc:
            # Deliberately non-fatal. The alt text itself is already generated
            # and written to job.results_csv_path, so a PDF that can't be
            # tagged -- most often one with no /StructTreeRoot for
            # embed_alt_text to splice into -- shouldn't throw that away. The
            # job still COMPLETEs and the reason rides along in error_message,
            # which is why a COMPLETE job can carry one (see job_store.py).
            #
            # SystemExit is named explicitly (it's a BaseException, so the
            # bare `Exception` above would miss it) as defence in depth:
            # embed_alt_text_into_pdf already converts the SystemExits that
            # embed() raises, but one escaping here would unwind worker.py's
            # polling loop and take the whole daemon down over one bad PDF.
            # KeyboardInterrupt is deliberately NOT caught -- Ctrl-C/SIGINT
            # should still stop the worker promptly.
            embed_warning = f"alt text generated, but embedding failed: {exc}"
            logging.exception("job %s: embedding failed", job.job_id)

        job_store.update_job_state(job.job_id, "COMPLETE", error=embed_warning)
    except Exception as exc:
        job_store.update_job_state(job.job_id, "FAILED", error=str(exc))
        logging.exception("job %s failed", job.job_id)
    finally:
        telegram_status.refresh_safely()
