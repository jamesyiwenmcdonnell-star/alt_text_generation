"""
test_job_paths.py

Regression tests for the one string in job_store that an outside caller
controls: pdf_filename. It arrives as an HTTP upload filename (POST /jobs),
Starlette hands it over verbatim, and every on-disk path for the job is built
from it -- so a name like '../../../../tmp/evil.pdf' used to resolve outside
the job directory, which api_server then open()ed for writing.

Run directly (no pytest in this project):
    python3 test_job_paths.py
"""

import os
import shutil
import sys
import tempfile

import job_store

PDF_BYTES = b"%PDF-1.4\n% minimal stand-in, never parsed by these tests\n"

TRAVERSAL_NAME = "../../../../tmp/evil.pdf"

# (client-supplied filename, expected stored filename)
FILENAME_CASES = [
    ("report.pdf",                 "report.pdf"),      # ordinary name survives untouched
    ("Fig 3 (final).pdf",          "Fig_3__final_.pdf"),
    (TRAVERSAL_NAME,               "evil.pdf"),
    ("....//....//evil.pdf",       "evil.pdf"),        # not fooled by a single-pass '../' strip
    ("/etc/passwd.pdf",            "passwd.pdf"),
    ("..\\..\\windows\\evil.pdf",  "evil.pdf"),        # backslashes, which basename() ignores on posix
    ("../../..",                   "__.pdf"),          # basename is '..' itself
    ("../",                        "job.pdf"),         # basename is empty -> _sanitize's fallback
    ("evil.pdf\x00.png",           "evil_pdf_.pdf"),   # NUL byte can't reach open()
]

failures = []


def check(label, condition, detail=""):
    if condition:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}{': ' + detail if detail else ''}")
        failures.append(label)


def test_safe_pdf_filename():
    print("safe_pdf_filename() reduces hostile names to a bare basename")
    for raw, expected in FILENAME_CASES:
        got = job_store.safe_pdf_filename(raw)
        check(f"{raw!r} -> {expected!r}", got == expected, f"got {got!r}")
        check(f"{raw!r} is idempotent", job_store.safe_pdf_filename(got) == got)
        check(f"{raw!r} yields no separator", os.sep not in got and got not in (".", ".."))


def test_create_job_keeps_pdf_path_inside_job_dir():
    print("create_job() stores a name whose pdf_path stays under job_dir")
    job = job_store.create_job(editor_id="jsmith", pdf_filename=TRAVERSAL_NAME)

    check("stored filename is sanitized", job.pdf_filename == "evil.pdf", f"got {job.pdf_filename!r}")
    check("pdf_path is inside job_dir",
          os.path.abspath(job.pdf_path).startswith(os.path.abspath(job.job_dir) + os.sep),
          f"got {job.pdf_path!r} for job_dir {job.job_dir!r}")
    check("job_dir is inside JOBS_ROOT",
          os.path.abspath(job.job_dir).startswith(os.path.abspath(job_store.JOBS_ROOT) + os.sep))
    check("job_id carries no separator", os.sep not in job.job_id and ".." not in job.job_id,
          f"got {job.job_id!r}")

    # The write api_server.submit_job performs, on the path it is handed.
    with open(job.pdf_path, "wb") as f:
        f.write(PDF_BYTES)
    landed = os.path.join(job.job_dir, "pdf", "evil.pdf")
    check("bytes land in the job's pdf dir", os.path.exists(landed))
    check("nothing was written to /tmp/evil.pdf", not os.path.exists("/tmp/evil.pdf"))

    # Round-tripping through sqlite must not reintroduce the raw name.
    reloaded = job_store.get_job(job.job_id)
    check("reloaded job agrees", reloaded.pdf_filename == "evil.pdf" and reloaded.pdf_path == job.pdf_path)


def test_pdf_path_rejects_a_traversing_row():
    """The backstop for a row that predates safe_pdf_filename() -- or was written
    by hand. pdf_path should raise rather than hand back an escaping path."""
    print("Job.pdf_path() refuses to escape job_dir even for a hand-forged row")
    forged = job_store.Job(
        job_id="forged", editor_id="jsmith", pdf_filename=TRAVERSAL_NAME,
        source_reference=None, state="QUEUED", error_message=None,
        job_dir=os.path.join(job_store.JOBS_ROOT, "forged"), pod_id=None,
        created_at="", updated_at="", started_at=None, completed_at=None,
    )
    try:
        path = forged.pdf_path
    except ValueError:
        check("traversing pdf_filename raises ValueError", True)
    else:
        check("traversing pdf_filename raises ValueError", False, f"returned {path!r}")

    ok = job_store.Job(**{**forged.__dict__, "pdf_filename": "report.pdf"})
    check("an ordinary filename still resolves",
          ok.pdf_path == os.path.join(ok.job_dir, "pdf", "report.pdf"))


def test_submit_job_endpoint():
    """The reported vector end to end: POST /jobs with a traversing filename."""
    print("POST /jobs with a traversing filename writes inside the job dir")
    try:
        from fastapi.testclient import TestClient
    except ImportError as exc:
        print(f"  skip  fastapi.testclient unavailable ({exc})")
        return

    import api_server

    with TestClient(api_server.app) as client:
        resp = client.post(
            "/jobs",
            data={"editor_id": "jsmith"},
            files={"file": (TRAVERSAL_NAME, PDF_BYTES, "application/pdf")},
        )
    check("submit succeeded", resp.status_code == 201, f"{resp.status_code} {resp.text}")
    if resp.status_code != 201:
        return

    body = resp.json()
    check("echoed filename is the sanitized one", body["pdf_filename"] == "evil.pdf",
          f"got {body['pdf_filename']!r}")

    job = job_store.get_job(body["job_id"])
    check("upload landed under the job dir", os.path.exists(job.pdf_path))
    check("upload landed with the right bytes", open(job.pdf_path, "rb").read() == PDF_BYTES)
    check("nothing escaped to /tmp/evil.pdf", not os.path.exists("/tmp/evil.pdf"))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="job_store_test_")
    # Point the store at a scratch tree so the tests never touch the real
    # jobs.db or jobs/ -- both are read from module globals at call time.
    job_store.JOBS_ROOT = os.path.join(tmp, "jobs")
    job_store.DB_PATH = os.path.join(tmp, "jobs.db")
    job_store.init_db()

    try:
        for test in (test_safe_pdf_filename,
                     test_create_job_keeps_pdf_path_inside_job_dir,
                     test_pdf_path_rejects_a_traversing_row,
                     test_submit_job_endpoint):
            test()
            print()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for name in failures:
            print(f"  - {name}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
