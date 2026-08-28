"""
api_server.py

HTTP intake API for the alt-text pipeline -- the "start and endpoints"
contract handed to the company's SharePoint integration developer. FastAPI's
auto-generated /docs and /openapi.json double as that integration spec.

No auth (internal network only, per confirmed scope). Runs alongside
worker.py as a second foreground process in the same container (entrypoint.sh);
this process only ever writes new QUEUED jobs and reads state -- all actual
pipeline work happens in worker.py, so a slow/stuck job can never block a
status request here.

Endpoints:
    POST /jobs                     -> submit a PDF, returns the new job
    GET  /jobs/{job_id}            -> one job's current state
    GET  /jobs                     -> paginated list, filterable by editor_id/state
    GET  /jobs/{job_id}/results    -> alt-text rows, once state == COMPLETE
    GET  /jobs/{job_id}/tagged-pdf -> the alt-text-embedded PDF, once state == COMPLETE
    GET  /health                   -> liveness check
"""

import csv
import logging
import os

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

import job_store
import telegram_status

MAX_UPLOAD_BYTES = 300 * 1024 * 1024  # 300MB -- raised from 100MB; still far above the observed corpus
                                      # (largest real PDF ~20MB). Uploads are read fully into memory
                                      # before this check (submit_job), so don't raise this further
                                      # without switching to a chunked read -- the API shares an 8GB
                                      # VM with the worker and pdffigures2's 4GB JVM heap.

app = FastAPI(
    title="Alt-Text Pipeline Intake API",
    description="Submit PDFs for automatic figure extraction and alt-text generation, and poll job status.",
    version="1.0",
)


class JobOut(BaseModel):
    job_id: str
    editor_id: str
    pdf_filename: str
    source_reference: str | None
    state: str
    error_message: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    completed_at: str | None
    # Embedding confidence: predicted coverage from the pre-generation dry run,
    # actual coverage after embedding (both 0..1, null until their stage has
    # run), and a human-readable note -- prefixed "LOW EMBED CONFIDENCE" when
    # the tagged PDF should not be trusted as complete.
    embed_precheck_coverage: float | None
    embed_coverage: float | None
    embed_note: str | None

    @classmethod
    def from_job(cls, job: job_store.Job) -> "JobOut":
        return cls(
            job_id=job.job_id, editor_id=job.editor_id, pdf_filename=job.pdf_filename,
            source_reference=job.source_reference, state=job.state, error_message=job.error_message,
            created_at=job.created_at, updated_at=job.updated_at,
            started_at=job.started_at, completed_at=job.completed_at,
            embed_precheck_coverage=job.embed_precheck_coverage,
            embed_coverage=job.embed_coverage, embed_note=job.embed_note,
        )


class JobListOut(BaseModel):
    jobs: list[JobOut]
    total: int


@app.on_event("startup")
def _startup() -> None:
    job_store.init_db()


def _refresh_board_safely() -> None:
    """Telegram board refresh for the intake path. The worker owns the board
    and refreshes it on every state change -- but while it's blocked inside a
    long synchronous stage (waiting up to ~7 minutes for a RunPod pod to boot),
    nothing refreshes, so jobs accepted during that window are queued in the DB
    yet invisible on the board and look lost. Refreshing here right after a
    submission closes that gap.

    Runs as a BackgroundTask (after the 201 is already sent). The swallowing
    itself now lives in telegram_status.refresh_safely(), which every caller in
    the pipeline uses for the same reason: by this point the job is committed
    to the DB, and the board is cosmetic -- a Telegram outage or missing bot
    credentials must never turn a successful upload into an error. Both
    processes editing the same board message is fine: each edit re-renders the
    full board from the DB, so last write wins and both writers produce the
    same content."""
    telegram_status.refresh_safely()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/jobs", response_model=JobOut, status_code=201)
async def submit_job(
    background_tasks: BackgroundTasks,
    editor_id: str = Form(..., description="Identity of the editor's SharePoint subfolder this PDF came from"),
    file: UploadFile = File(..., description="The PDF to process"),
    source_reference: str | None = Form(None, description="Opaque passthrough id (e.g. a SharePoint item id) -- stored and echoed back, never interpreted"),
) -> JobOut:
    if not editor_id.strip():
        raise HTTPException(400, "editor_id must not be empty")
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "file must have a .pdf filename")

    content = await file.read()
    if not content:
        raise HTTPException(400, "uploaded file is empty")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"file exceeds the {MAX_UPLOAD_BYTES} byte limit")
    if not content.startswith(b"%PDF-"):
        raise HTTPException(400, "file content doesn't look like a PDF (missing %PDF- header)")

    # create_job() normalizes the filename (safe_pdf_filename) before storing it --
    # file.filename is client-supplied and Starlette doesn't sanitize it, so it must
    # never reach a path join as-is. The JobOut below echoes the stored name.
    job = job_store.create_job(editor_id=editor_id, pdf_filename=file.filename, source_reference=source_reference)
    with open(job.pdf_path, "wb") as f:
        f.write(content)

    # After the response is sent, not inline: the refresh is an outbound HTTP
    # call (up to ~15s on a bad day) and the submitter shouldn't wait on it.
    background_tasks.add_task(_refresh_board_safely)

    return JobOut.from_job(job)


@app.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str) -> JobOut:
    job = job_store.get_job(job_id)
    if job is None:
        raise HTTPException(404, f"no job with id {job_id!r}")
    return JobOut.from_job(job)


@app.get("/jobs", response_model=JobListOut)
def list_jobs(editor_id: str | None = None, state: str | None = None, limit: int = 50, offset: int = 0) -> JobListOut:
    jobs = job_store.list_jobs(editor_id=editor_id, state=state, limit=limit, offset=offset)
    total = job_store.count_jobs(editor_id=editor_id, state=state)
    return JobListOut(jobs=[JobOut.from_job(j) for j in jobs], total=total)


@app.get("/jobs/{job_id}/results")
def get_job_results(job_id: str) -> list[dict]:
    job = job_store.get_job(job_id)
    if job is None:
        raise HTTPException(404, f"no job with id {job_id!r}")
    if job.state != "COMPLETE":
        raise HTTPException(409, f"job {job_id} is {job.state}, not COMPLETE yet")

    with open(job.results_csv_path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


@app.get("/jobs/{job_id}/tagged-pdf", response_class=FileResponse)
def get_tagged_pdf(job_id: str) -> FileResponse:
    """The source PDF with the generated alt text embedded as real <Figure>
    structure elements. A job can be COMPLETE without one -- embedding is
    non-fatal, so if it failed the alt text is still available from
    /jobs/{job_id}/results -- hence the 404 carrying error_message."""
    job = job_store.get_job(job_id)
    if job is None:
        raise HTTPException(404, f"no job with id {job_id!r}")
    if job.state != "COMPLETE":
        raise HTTPException(409, f"job {job_id} is {job.state}, not COMPLETE yet")
    if not os.path.exists(job.tagged_pdf_path):
        reason = job.error_message or "no tagged PDF was produced"
        raise HTTPException(404, f"job {job_id} has no tagged PDF: {reason}")

    # If the post-embed coverage check flagged this job, say so in the download
    # filename itself -- a consumer that only checks state == COMPLETE and
    # never reads embed_note/embed_coverage must still be unable to mistake a
    # partially tagged file for a complete one. Gated on embed_coverage being
    # set (not just the note) because the pre-generation precheck also writes a
    # LOW note but leaves embed_coverage NULL -- only the real, measured
    # post-embed verdict should rename the file. The on-disk name is untouched.
    filename = os.path.basename(job.tagged_pdf_path)
    if (job.embed_coverage is not None and job.embed_note
            and job.embed_note.startswith(job_store.LOW_CONFIDENCE_PREFIX)):
        filename = f"PARTIALLY TAGGED PDF - {filename}"

    return FileResponse(
        job.tagged_pdf_path,
        media_type="application/pdf",
        filename=filename,
    )
