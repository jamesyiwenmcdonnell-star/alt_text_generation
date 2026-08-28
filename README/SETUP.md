# Project setup — alt-text pipeline

The pipeline runs as a persistent Docker daemon: an intake API and a background worker,
generating alt text on a rented RunPod GPU and embedding it back into the PDF. Everything
Java/Python-side runs inside one container image, so the only things installed directly on
your Mac are Homebrew, Docker (via Colima), and git.

For day-to-day operation once this is done once, see [COMMANDS.md](COMMANDS.md). For what the
system actually does in production, see [GENERAL_BEHAVIORS.md](GENERAL_BEHAVIORS.md). For the
embedding stage specifically, see [EMBEDDING_behaviour.md](EMBEDDING_behaviour.md).

## 1. Install

```bash
brew install docker colima git
colima start --cpu 4 --memory 8
```

`colima start` needs to be re-run after every reboot (it doesn't auto-start like Docker Desktop
would). Check it's up with `docker info` before continuing — it should return engine info with
no connection errors.

## 2. Credentials

The worker refuses to start without all four of these set in your shell environment — not
hardcoded anywhere, since `startup.py` forwards them from the host into the container:

```bash
export RUNPOD_API_KEY="your-runpod-key"      # rents, polls and terminates the GPU pod
export HF_TOKEN="your-hf-token"              # forwarded into the pod's environment to pull the
                                              # InternVL3.5-8B model weights on first boot
export TELEGRAM_BOT_TOKEN="your-bot-token"   # from @BotFather; posts the live status board
export TELEGRAM_CHAT_ID="your-chat-id"       # the channel/chat the bot posts the board into
```

Quote the value with plain straight quotes or not at all. Quotes pasted from a document or chat
app are often curly (`"..."`), and those become *part of the value* rather than shell syntax —
a token wrapped in them produces a malformed API URL and an unhelpful 404.

### Creating the RunPod key (`RUNPOD_API_KEY`)

1. Sign up at [runpod.io](https://www.runpod.io/), then add credit under **Billing**. Pods bill
   per second against a prepaid balance, and pod creation fails outright on a zero balance.
2. Go to **Settings → API Keys → Create API Key**.
3. Give it **read/write** permission. The pipeline doesn't only read pod status — it creates and
   terminates pods (`pod.py`), so a read-only key gets as far as the first job and then fails.
4. Copy it there and then; RunPod shows the full key only once, at creation.

### Creating the Hugging Face token (`HF_TOKEN`)

1. Sign up at [huggingface.co](https://huggingface.co/), then go to
   **Settings → Access Tokens → Create new token**.
2. A **Read** token is sufficient — it only ever downloads model weights, never uploads.
3. While logged in, open the model page for
   [`OpenGVLab/InternVL3_5-8B`](https://huggingface.co/OpenGVLab/InternVL3_5-8B) and accept the
   access conditions if that page asks for them. If the model is gated and the token's account
   hasn't been granted access, the pod boots normally and only fails partway through the weight
   download — a slow, confusing failure several minutes into a job rather than at startup.
4. Note where this token ends up: `controller.py` places it in the *pod's* environment
   (`RUNPOD_POD_CONFIG["env"]`) so vLLM on the rented machine can authenticate to Hugging Face.
   It doesn't stay on your Mac. Scope it read-only accordingly.

`gpu_ids_snapshot.txt` and `gpu_types.json` (candidate GPU types, cheapest-first) already ship
in the repo with real RunPod ids — nothing to generate for a fresh setup. If RunPod's GPU
catalog changes later, `gpu_utils.check_for_id_drift()` / `list_all_gpu_ids()` refresh them.

## 3. How the RunPod pod works

Nothing model-related is installed on your Mac. A "pod" is a GPU container rented from RunPod
for as long as it's alive, and `controller.py` boots a stock `vllm/vllm-openai:v0.10.1` image on
one, serving `OpenGVLab/InternVL3_5-8B` behind an OpenAI-compatible HTTP API on port 8000
(exposed as `8000/http`). `runpod_VL.py` then posts each extracted figure to that endpoint and
gets alt text back. Both credentials above feed this single step: the RunPod key rents the
machine, the HF token lets vLLM inside it download the weights.

**Which GPU it picks.** `gpu_utils.pick_available_gpu()` walks `gpu_ids_snapshot.txt` in file
order — A40, L40, RTX A6000, RTX 6000 Ada — and takes the first one RunPod reports stock for.
`High`, `Medium` and `Low` all count as allocatable: rejecting `Low` doesn't find something
better, it just skips a GPU that would have worked, and every candidate sitting at `Low` is
routine. File order *is* the preference order, so preferring a different card means reordering
that file. If nothing has capacity the job fails with "No available GPU among the candidates",
which is a RunPod capacity problem rather than a bug in the pipeline.

**Booting takes minutes, not seconds.** `Pod.wait_for_pod()` polls for up to 420s (7 minutes).
A pod RunPod reports as `RUNNING` is *not* yet usable — vLLM still has to pull and load ~8B
parameters of weights. Readiness means the pod's own `/health` endpoint answers, which is why
`app_startup_pod_checker()` keeps returning `STARTING` long after the pod visibly exists.

**One pod, reused across jobs.** At the expected volume (≤10 PDFs/day) a fresh pod per job would
spend most of its life booting, so a single pod is shared. `ensure_pod_ready()` reuses the saved
one when it's both inside `POD_MAX_LIFETIME_S` (currently **1 hour**, set in `job_pipeline.py`)
and still healthy on a live status check — RunPod can evict a pod underneath you, so the cached
state is never trusted. Otherwise it retires that pod and starts a fresh one. By design there is
never more than one pod alive.

**Two ways it gets retired.** Lazily, when the next job notices the TTL has passed; or
proactively, on the worker's idle tick (`expired_pod_or_none()` → `retire_pod()`) so an unused
pod isn't left billing until some future job happens to ask for one. Separately,
`worker.py`'s `recover_from_crash()` terminates every live pod at startup, on the assumption
that anything still running after a restart is leftover from a crash.

**It survives a daemon restart.** `pod_state.txt` (written by `controller.save_pod()`) holds the
pod id, port and creation time, so a restarted worker reconnects to a still-warm pod instead of
orphaning it and renting a second one alongside.

> **Billing.** A pod charges for wall-clock time from creation to termination, not per request —
> an idle pod costs the same as a busy one. That's why a failed termination doesn't just get
> logged but raises its own Telegram alert (`alert_orphan_pod`): a leaked pod bills silently
> until somebody notices. If anything looks off, check the RunPod console directly; it is the
> only authoritative view of what you're actually paying for.

## 4. File structure

```
PROJECT/
├── docker/pdffigures2-build/
│   ├── Dockerfile          # JDK 17 + sbt + python3 + pikepdf/fastapi/etc + JAI JPEG2000 plugin
│   ├── build.sh            # builds pdffigures2.jar via the container
│   ├── shell.sh             # interactive dev shell inside the container (manual extraction/debugging)
│   └── entrypoint.sh        # daemon entrypoint: api_server.py + worker.py as two foreground processes
├── pdffigures2/             # git clone of allenai/pdffigures2 (built jar lands here)
├── PDFTesting/               # local test PDFs
├── startup.py               # one-command host entry point -- see "First start" below
├── api_server.py            # HTTP intake API (POST /jobs, GET /jobs/{id}, ...)
├── worker.py                 # job-queue daemon: claims QUEUED jobs, drives job_pipeline.process_job()
├── job_pipeline.py           # per-job orchestration: extract -> pod -> generate -> embed
├── job_store.py              # sqlite-backed job queue (jobs.db)
├── pdf_batch_runner.py       # extraction orchestrator: runs pdffigures2, builds manifest + validation report
├── embed_alt_text.py         # splices generated alt text into the PDF's structure tree
├── controller.py / pod.py / gpu_utils.py   # RunPod pod lifecycle (start/reuse/terminate, GPU selection)
├── runpod_VL.py               # talks to the vLLM pod's OpenAI-compatible endpoint for generation
├── telegram_status.py         # live status board via the Telegram Bot API
├── clean.py                   # destructive full-reset utility (see COMMANDS.md)
├── gpu_ids_snapshot.txt / gpu_types.json   # RunPod GPU candidates, preference order
├── jobs.db                    # generated on first run: sqlite job queue
├── jobs/                      # generated per submission: jobs/<job_id>/{pdf,extract_out,...}
└── pdffigures2_out/            # generated by manual/dev extraction runs (see step 6) -- NOT what the
                                  # daemon uses for real jobs; each job gets its own jobs/<job_id>/extract_out/
```

`docker/pdffigures2-build/*` and the `.py` files are delivered with the project; `pdffigures2/pdffigures2.jar`,
`jobs.db`, and `jobs/` are generated, not something to create by hand.

## 5. One-time build

```bash
cd PROJECT

# get the source
git clone https://github.com/allenai/pdffigures2.git pdffigures2

# known build fix: the sbt-bintray plugin references a dead service (Bintray
# shut down in 2021); strip it and the settings that depend on it -- neither
# is used by the `assembly` task this project actually needs
sed -i '' '/sbt-bintray/d' pdffigures2/project/plugins.sbt
sed -i '' '/bintray/d' pdffigures2/build.sbt

# build pdffigures2.jar inside the container (also builds the pdffigures2-builder
# image first if needed -- Docker's layer cache makes a repeat build a fast no-op)
chmod +x docker/pdffigures2-build/build.sh docker/pdffigures2-build/shell.sh
./docker/pdffigures2-build/build.sh
```

Confirm it worked: `ls pdffigures2/pdffigures2.jar` should exist. This step is also run
automatically by `startup.py` (step 6) if the jar is missing, so it's safe to skip straight
there on a fresh clone — this section exists for when the automatic path fails and you need to
see what it's actually doing, or when you're rebuilding after patching pdffigures2's Scala
source yourself (see the note at the end of this section).

**After changing pdffigures2's source** (e.g. tuning a constant in `CaptionDetector.scala`):
delete or rebuild over the existing jar and re-run `build.sh` — `startup.py` only builds when
`pdffigures2/pdffigures2.jar` is *missing*, so it will not notice a source edit and rebuild for
you. Keep a backup of the working jar (`cp pdffigures2/pdffigures2.jar pdffigures2/pdffigures2.jar.bak`)
before rebuilding, and re-run extraction on a known corpus afterward to check for regressions —
`build.sh`'s dependency cache (a Docker volume) makes a repeat build fast.

## 6. First start

```bash
python3 startup.py
```

This is the actual entry point — it checks Colima is running, builds the jar if missing,
(re)builds the `pdffigures2-builder` image, and launches the daemon as a container named
`alttext-pipeline` (`--restart unless-stopped`), running `api_server.py` and `worker.py` side by
side per `entrypoint.sh`. Safe to re-run any time: it does nothing if the daemon is already up,
and replaces it if found stopped.

```bash
curl http://localhost:8000/health
open http://localhost:8000/docs
```

`/docs` is the live OpenAPI spec for the intake API. From here, submitting a PDF (`POST /jobs`)
is what actually drives the pipeline — see [COMMANDS.md](COMMANDS.md) for the full request/response
walkthrough, polling, and container management.

### Manual/dev extraction (no daemon, no job queue)

For iterating on `pdf_batch_runner.py`/`embed_alt_text.py` themselves, or checking a new PDF's
extraction quality before submitting it as a real job, drop into an interactive shell instead:

```bash
./docker/pdffigures2-build/shell.sh
```

This mounts the whole project at `/work` inside a disposable container (`--rm` — nothing is lost
on `exit`, since all real files live in the mounted project folder). From there:

```bash
python3 pdf_batch_runner.py \
  --input-dir ./PDFTesting \
  --jar ./pdffigures2/pdffigures2.jar \
  --output-dir ./pdffigures2_out \
  --dpi 300 --java-heap 6g -v
```

Re-running after adding more PDFs to `PDFTesting/` doesn't require rebuilding anything — just
add `--skip-done` to only process the new ones:

```bash
python3 pdf_batch_runner.py --input-dir ./PDFTesting --jar ./pdffigures2/pdffigures2.jar --output-dir ./pdffigures2_out --skip-done
```

This path is for extraction only. It has no GPU pod, generates no alt text, and never touches
`jobs.db` — for the full generate-and-embed pipeline, submit through the API instead.

## 7. What to check afterward

- **The Telegram board** — one continuously-edited message showing every job's live state.
- **`GET /jobs/{id}`** — a job's current state, plus `embed_precheck_coverage` / `embed_coverage` /
  `embed_note` (see [EMBEDDING_behaviour.md](EMBEDDING_behaviour.md) for what these mean and when
  a PDF is flagged as unreliable to embed).
- **`pdffigures2_out/manifest.csv`** (manual runs only) — every detected figure/table, with image
  path, page, caption, bounding box.
- **`pdffigures2_out/validation_report.csv`** (manual runs only) — flags gaps in figure numbering
  (e.g. `1.1, 1.3` with no `1.2`), a signal pdffigures2 likely missed a real figure.

## Known gaps in this setup path

- No automated check that `RUNPOD_API_KEY`/`HF_TOKEN`/etc. are actually *valid*, only that
  they're *set* — an expired or mistyped key surfaces later as a job failure, not at startup.
- `docker/pdffigures2-build/shell.sh` and `startup.py` both call `ensure_image_built()`-equivalent
  logic independently; there's no single source of truth for "is the image up to date" beyond
  Docker's own layer cache.
