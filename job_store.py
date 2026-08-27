"""
job_store.py

sqlite3-backed job queue for the multi-PDF alt-text pipeline. Flat files
(like pod_state.txt) work for single-scalar state but can't answer "give me
the oldest queued job" or support two processes (api_server.py, worker.py)
writing concurrently without hand-rolled locking -- sqlite gives that for
free via PRAGMA journal_mode=WAL, at zero new dependency cost (stdlib).

Each job's on-disk files live under JOBS_ROOT/<job_id>/, per the fixed
layout the Job dataclass's properties below encode:
    jobs/<job_id>/pdf/<pdf_filename>
    jobs/<job_id>/extract_out/{figures,data,stats,logs}/  manifest.csv  validation_report.csv
    jobs/<job_id>/alt_text_results.csv
    jobs/<job_id>/<pdf_stem>_tagged.pdf
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(PROJECT_ROOT, "jobs.db")
JOBS_ROOT = os.path.join(PROJECT_ROOT, "jobs")

VALID_STATES = ("QUEUED", "EXTRACTING", "EXTRACTED", "POD_STARTING", "GENERATING",
                "EMBEDDING", "COMPLETE", "FAILED")
IN_PROGRESS_STATES = ("EXTRACTING", "EXTRACTED", "POD_STARTING", "GENERATING", "EMBEDDING")
TERMINAL_STATES = ("COMPLETE", "FAILED")

# A COMPLETE job normally has error_message NULL. It's non-NULL only when a
# non-fatal step failed after the alt text itself was already produced --
# currently just embedding (see job_pipeline.process_job) -- so callers should
# read error_message on a COMPLETE job as "delivered, with a caveat", not as
# a failure.


@dataclass
class Job:
    job_id: str
    editor_id: str
    pdf_filename: str
    source_reference: str | None
    state: str
    error_message: str | None
    job_dir: str
    pod_id: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    completed_at: str | None

    @property
    def pdf_dir(self) -> str:
        return os.path.join(self.job_dir, "pdf")

    @property
    def pdf_path(self) -> str:
        return _join_within(self.pdf_dir, self.pdf_filename)

    @property
    def extract_dir(self) -> str:
        return os.path.join(self.job_dir, "extract_out")

    @property
    def manifest_path(self) -> str:
        return os.path.join(self.extract_dir, "manifest.csv")

    @property
    def validation_path(self) -> str:
        return os.path.join(self.extract_dir, "validation_report.csv")

    @property
    def figures_dir(self) -> str:
        return os.path.join(self.extract_dir, "figures")

    @property
    def results_csv_path(self) -> str:
        return os.path.join(self.job_dir, "alt_text_results.csv")

    @property
    def pdf_stem(self) -> str:
        """The value this PDF's rows carry in the manifest's pdf_name column.
        pdf_batch_runner.build_manifest() derives that from pdffigures2's
        per-doc JSON filename, which is itself the PDF's stem -- so the same
        expression is what joins manifest rows, alt-text rows and this job's
        PDF back together."""
        return os.path.splitext(self.pdf_filename)[0]

    @property
    def tagged_pdf_path(self) -> str:
        """Where embedding writes the accessible copy. Deliberately in job_dir
        itself rather than pdf_dir, so it can never collide with the source PDF
        -- embed_alt_text.embed() refuses to write over its own input."""
        return os.path.join(self.job_dir, f"{self.pdf_stem}_tagged.pdf")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sanitize(value: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_" else "_" for c in value)
    return cleaned or "job"


def safe_pdf_filename(pdf_filename: str) -> str:
    """Reduces a client-supplied upload filename to a bare, path-safe basename.

    Uploads reach create_job() straight from the wire (api_server.submit_job),
    and Starlette passes the browser-supplied filename through verbatim -- so it
    may be absolute, use backslash separators, or contain '..'. Since the stored
    pdf_filename is what every downstream path is built from, it's normalized
    once here rather than defended against at each use. Idempotent, and always
    returns a name ending in .pdf (create_job's only caller already rejects
    anything else)."""
    base = os.path.basename(pdf_filename.replace("\\", "/"))
    return _sanitize(os.path.splitext(base)[0]) + ".pdf"


def _join_within(base: str, *parts: str) -> str:
    """os.path.join(), but refuses to return a path that escapes base.

    A backstop under safe_pdf_filename(): rows written before it existed (or by
    hand) could still hold a traversing pdf_filename, and callers open() these
    paths for writing. Fails loudly rather than silently writing outside the
    job directory."""
    path = os.path.join(base, *parts)
    base_abs, path_abs = os.path.abspath(base), os.path.abspath(path)
    if base_abs != path_abs and os.path.commonpath([base_abs, path_abs]) != base_abs:
        raise ValueError(f"path {path!r} escapes {base!r}")
    return path


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    os.makedirs(JOBS_ROOT, exist_ok=True)
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                editor_id TEXT NOT NULL,
                pdf_filename TEXT NOT NULL,
                source_reference TEXT,
                state TEXT NOT NULL,
                error_message TEXT,
                job_dir TEXT NOT NULL,
                pod_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_state_created ON jobs(state, created_at)")


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(**{k: row[k] for k in row.keys()})


def create_job(editor_id: str, pdf_filename: str, source_reference: str | None = None) -> Job:
    """Creates a new QUEUED job: DB row + its jobs/<job_id>/pdf/ directory.
    The caller still needs to write the uploaded PDF bytes to job.pdf_path.

    pdf_filename is normalized by safe_pdf_filename() before it's stored, so
    job.pdf_filename is always a plain basename. The stored name is the one that
    lands on disk, which keeps it in step with the pdf_name the extraction step
    later reports for the same file."""
    pdf_filename = safe_pdf_filename(pdf_filename)
    stem = os.path.splitext(pdf_filename)[0]
    editor_safe = _sanitize(editor_id)
    job_id = f"{time.strftime('%Y%m%d%H%M%S', time.gmtime())}_{editor_safe}_{stem}_{uuid.uuid4().hex[:8]}"
    job_dir = os.path.join(JOBS_ROOT, job_id)
    os.makedirs(os.path.join(job_dir, "pdf"), exist_ok=True)

    now = _now()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO jobs (job_id, editor_id, pdf_filename, source_reference, state,
                                  error_message, job_dir, pod_id, created_at, updated_at,
                                  started_at, completed_at)
               VALUES (?, ?, ?, ?, 'QUEUED', NULL, ?, NULL, ?, ?, NULL, NULL)""",
            (job_id, editor_id, pdf_filename, source_reference, job_dir, now, now),
        )
    return get_job(job_id)


def get_job(job_id: str) -> Job | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def list_jobs(editor_id: str | None = None, state=None, limit: int = 50, offset: int = 0) -> list[Job]:
    """state may be a single state string or an iterable of states."""
    clauses, params = [], []
    if editor_id is not None:
        clauses.append("editor_id = ?")
        params.append(editor_id)
    if state is not None:
        states = [state] if isinstance(state, str) else list(state)
        clauses.append(f"state IN ({','.join('?' * len(states))})")
        params.extend(states)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])

    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ? OFFSET ?", params
        ).fetchall()
    return [_row_to_job(r) for r in rows]


def list_jobs_completed_before(state: str, cutoff_iso: str) -> list[Job]:
    """Jobs in the given state whose completed_at is older than cutoff_iso
    (a UTC ISO8601 string, same format _now() produces) -- e.g. for finding
    FAILED jobs whose on-disk files are due for cleanup."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE state = ? AND completed_at IS NOT NULL AND completed_at < ?",
            (state, cutoff_iso),
        ).fetchall()
    return [_row_to_job(r) for r in rows]


def count_jobs(editor_id: str | None = None, state=None) -> int:
    """Total matching jobs regardless of limit/offset -- for list_jobs()'s
    pagination metadata, since len(list_jobs(...)) only reflects one page."""
    clauses, params = [], []
    if editor_id is not None:
        clauses.append("editor_id = ?")
        params.append(editor_id)
    if state is not None:
        states = [state] if isinstance(state, str) else list(state)
        clauses.append(f"state IN ({','.join('?' * len(states))})")
        params.extend(states)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with _connect() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM jobs {where}", params).fetchone()
    return row["n"]


def claim_next_queued_job() -> Job | None:
    """Atomically picks the oldest QUEUED job and marks it EXTRACTING in one
    transaction, so a second worker process (if one ever runs) can't
    double-claim it. The caller should NOT call update_job_state(..., "EXTRACTING")
    again -- that transition already happened here."""
    now = _now()
    with _connect() as conn:
        row = conn.execute(
            "SELECT job_id FROM jobs WHERE state = 'QUEUED' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        job_id = row["job_id"]
        conn.execute(
            "UPDATE jobs SET state='EXTRACTING', started_at=?, updated_at=? WHERE job_id=?",
            (now, now, job_id),
        )
        job_row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return _row_to_job(job_row)


def update_job_state(job_id: str, state: str, error: str | None = None) -> None:
    if state not in VALID_STATES:
        raise ValueError(f"unknown job state: {state!r}")
    now = _now()
    completed_at = now if state in TERMINAL_STATES else None
    with _connect() as conn:
        conn.execute(
            "UPDATE jobs SET state=?, error_message=?, updated_at=?, "
            "completed_at=COALESCE(?, completed_at) WHERE job_id=?",
            (state, error, now, completed_at, job_id),
        )


def set_pod_id(job_id: str, pod_id: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE jobs SET pod_id=?, updated_at=? WHERE job_id=?", (pod_id, _now(), job_id))
