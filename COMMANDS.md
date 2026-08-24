# Command reference

Quick reference for operating the alt-text pipeline. For first-time host setup (Colima, pdffigures2 checkout, jar build), see [SETUP.md](SETUP.md) — everything below assumes that's already done once.

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
curl -X POST http://localhost:8000/jobs -F "editor_id=jsmith" -F "file=@PDFTesting/14653.pdf;type=application/pdf"
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

## Cleanup

Stop and remove the daemon entirely:

```bash
docker rm -f alttext-pipeline
```

Remove the built image, forcing a full rebuild next time:

```bash
docker image rm pdffigures2-builder
```
