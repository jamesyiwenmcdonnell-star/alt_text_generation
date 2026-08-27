# Command reference

Quick reference for operating the alt-text pipeline. For first-time host setup (Colima, pdffigures2 checkout, jar build), see [SETUP.md](SETUP.md) — everything below assumes that's already done once. For how the embedding stage decides what it can tag and how it reports incomplete coverage, see [EMBEDDING.md](EMBEDDING.md).

## One-time setup

Install prerequisites:

```bash
brew install docker colima git
```

Everything else — starting Colima, building pdffigures2's jar, building the Docker image — is handled automatically by `startup.py`, but standalone if you ever need them:

```bash
colima start --cpu 4 --memory 8
```

```bash
git clone https://github.com/allenai/pdffigures2.git pdffigures2
```

```bash
docker build -t pdffigures2-builder ./docker/pdffigures2-build
```

## Starting the pipeline daemon

Export credentials in your own shell — never hardcode these in source files, since `startup.py` forwards them from the host environment into the container:

```bash
export RUNPOD_API_KEY="your-runpod-key"
export HF_TOKEN="your-hf-token"
export TELEGRAM_BOT_TOKEN="your-bot-token"
export TELEGRAM_CHAT_ID="your-chat-id"
```

Then:

```bash
python3 startup.py
```

Safe to re-run any time — it detects an already-running daemon and skips recreating it, or replaces a stopped one.

## Health / status checks

```bash
curl http://localhost:8000/health
```

```bash
open http://localhost:8000/docs
```

`/docs` is the live, auto-generated OpenAPI spec — the source of truth for the API contract.

## Using the API

Submit a PDF:

```bash
curl -X POST http://localhost:8000/jobs -F "editor_id=Nee_phua" -F "file=@PDFTesting/full_test_4.pdf;type=application/pdf"
```

Check one job's status:

```bash
curl http://localhost:8000/jobs/<job_id>
```

List jobs (filterable by `editor_id`/`state`, paginated):

```bash
curl "http://localhost:8000/jobs?editor_id=jsmith&state=FAILED&limit=20"
```

Get a completed job's alt text (`409` if not `COMPLETE` yet):

```bash
curl http://localhost:8000/jobs/<job_id>/results
```

Poll a job's status every few seconds without `watch` (not preinstalled on macOS):

```bash
while true; do clear; curl -s http://localhost:8000/jobs/<job_id> | python3 -m json.tool; sleep 3; done
```

Install the real `watch` instead, if you want it generally available:

```bash
brew install watch
```

## Container management

```bash
docker ps -a --filter name=alttext-pipeline
```

```bash
docker logs -f alttext-pipeline
```

```bash
docker logs alttext-pipeline --tail 60
```

Restart to pick up a Python code change — no rebuild needed, the project directory is bind-mounted live:

```bash
docker restart alttext-pipeline
```

Force a full recreate — needed after a `Dockerfile` change (new dependency), or to clear a stuck container:

```bash
docker rm -f alttext-pipeline && python3 startup.py
```

```bash
docker stop alttext-pipeline
```

## Debugging inside the container

```bash
docker exec -it alttext-pipeline bash
```

```bash
docker exec alttext-pipeline ps aux
```

Check env vars actually made it in (prints secrets — redact before sharing output):

```bash
docker exec alttext-pipeline env | grep -E "RUNPOD|TELEGRAM|HF_TOKEN"
```

Inspect the job queue directly:

```bash
docker exec alttext-pipeline python3 -c "import job_store; [print(j) for j in job_store.list_jobs(limit=50)]"
```

Manually sweep stray RunPod pods (also happens automatically on worker startup):

```bash
docker exec alttext-pipeline python3 -c "import controller; controller.terminate_all_pods(controller.RUNPOD_API_KEY)"
```

## Interactive dev shell (separate from the running daemon)

For manually running `pdf_batch_runner.py`/`diagnose_tagging.py` or poking around outside the queue system:

```bash
./docker/pdffigures2-build/shell.sh
```

## Checking embedding coverage

Predict how much of a PDF can be tagged, without writing anything and without needing alt text — the same check the pipeline runs automatically before generation (see [EMBEDDING.md](EMBEDDING.md)). Useful for vetting a new document, or a new publisher's documents, before committing GPU time:

```bash
docker run --rm -v "$(pwd):/work" -w /work --entrypoint python3 pdffigures2-builder embed_alt_text.py --pdf PDFTesting/<file>.pdf --manifest pdffigures2_out/manifest.csv --fallback-caption --dry-run
```

Inspect the coverage recorded on jobs that already ran:

```bash
docker exec alttext-pipeline python3 -c "import job_store; [print(f'{j.pdf_filename}: precheck={j.embed_precheck_coverage} actual={j.embed_coverage} {j.embed_note}') for j in job_store.list_jobs(limit=50)]"
```

Verify a tagged PDF's alt text on the command line (no Acrobat needed):

```bash
docker run --rm -v "$(pwd):/work" -w /work --entrypoint python3 pdffigures2-builder alt_text_validation.py --pdf jobs/<job_id>/<stem>_tagged.pdf --alt-preview 5
```

## Local filesystem checks (on the host, no Docker needed)

```bash
ls -la jobs/
```

```bash
cat pod_state.txt
```

```bash
cat telegram_message_id.txt
```

## Full reset (destructive — irreversible)

`clean.py` permanently deletes **every** job's files and DB record, and resets the Telegram status board to a clean starting state (the board message is edited in place, not deleted, so a still-running worker keeps updating it instead of posting a new one; any other tracked alert messages are deleted). It prints what it's about to do, warns if any job isn't in a terminal state yet, and requires typing `DELETE ALL` exactly before doing anything — any other input aborts with nothing touched.

Stop the daemon first — running this while `worker.py` is actively mid-job risks deleting a job's files or DB row out from under it:

```bash
docker stop alttext-pipeline
```

```bash
docker exec -it alttext-pipeline python3 clean.py
```

## Cleanup

Stop and remove the daemon entirely:

```bash
docker rm -f alttext-pipeline
```

Remove the built image, forcing a full rebuild next time:

```bash
docker image rm pdffigures2-builder
```
