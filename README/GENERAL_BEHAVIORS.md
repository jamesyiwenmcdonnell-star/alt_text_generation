# Key system behaviors

Reference doc for how the alt-text pipeline actually behaves in production —
written for stakeholders who need to understand what the system does and
what guardrails exist, not how to operate it day-to-day (see
[COMMANDS.md](COMMANDS.md)) or set it up (see [SETUP.md](SETUP.md)). The
embedding stage has its own deep reference in [EMBEDDING_behaviour.md](EMBEDDING_behaviour.md),
including how it measures and reports incomplete tagging.

## What it does

Editors submit a PDF via HTTP. The system extracts every figure,
generates screen-reader alt text for each one using a vision-language model,
and embeds that alt text back into the PDF as real, screen-reader-visible
structure — then makes both the raw alt-text data and the tagged PDF
available for download. **Figures only — tables are not deliverables**: a
prose description on a `<Figure>` tag is the wrong accessibility structure
for a data table (it needs navigable `<Table>/<TR>/<TD>` markup), so tables
are filtered out at the manifest and never generated, embedded, or counted
toward coverage (`DELIVERABLE_FIG_TYPES` in `pdf_batch_runner.py`). One editor-facing intake API; the actual work runs
as a background pipeline.

## Pipeline stages

Each submitted PDF moves through a fixed sequence of states, tracked per-job
in a database: `QUEUED → EXTRACTING → EXTRACTED → POD_STARTING → GENERATING
→ EMBEDDING → COMPLETE` (or `FAILED` at any point).

1. **Extraction** — `pdffigures2` (an open-source Java tool) locates every
   figure and table in the PDF and produces a manifest (image crops + page/
   caption/bounding-box metadata); tables are then dropped from the manifest
   as non-deliverables (their crops still land on disk, unreferenced).
   PDFS ARE LIMITED TO 300MB
2. **Pod startup** — a GPU pod is provisioned on RunPod (cloud GPU rental) to
   serve the vision-language model, or an already-running one is reused (see
   below).
3. **Generation** — the model (InternVL3.5-8B) describes every extracted
   figure and writes one alt-text string per figure to a results CSV.
4. **Embedding** — the alt text is spliced into a copy of the source PDF as
   proper `<Figure>` structure elements (not just a metadata field) so
   screen readers actually announce it.

## Processing model: one job at a time

Jobs are processed strictly sequentially, one PDF fully through the pipeline
before the next starts. This matches confirmed volume (≤10 PDFs/day) — there
is currently no parallelism and no priority queue; jobs run in the order
they were submitted.

## GPU pod lifecycle

Cold-starting a fresh GPU pod takes 5–7 minutes, which would dominate
runtime if done per job at this volume. Instead:

- One pod is shared across jobs and kept alive for up to its lifetime cap
  (~1 hour), reused for every new job that arrives within that window.
- A pod nearing/past that cap is retired (terminated) automatically, either
  when the next job needs one or proactively during idle polling — nothing
  sits billing unused indefinitely.
- If RunPod's preferred GPU type (A40) has no capacity, the system falls
  back through a preference-ordered list of alternative GPU types rather
  than failing the job.
- On worker startup (e.g. after a crash or restart), any pod left running
  from before is swept and terminated — the design assumes at most one live
  pod at a time, so anything found alive is leftover, not in-use.
- **Termination failures are treated as a billing risk**, not just a log
  line — they trigger a dedicated, separate Telegram alert distinct from
  routine status updates.
- **Costs per hour** InternVL3.5-8B costs $0.44 USD per hr of usage if the 
  Nividia A40 GPU is used. If this GPU is unavailable when starting up a pod, 
  the handeler will switch to the next compatible and cheapest option. The 
  maximum cost the handler will spend is $1.09 per hr. 

## Failure handling

- **Crash recovery**: any job left mid-pipeline when the worker restarts is
  marked `FAILED` rather than silently resumed — partial output on disk may
  be stale, so a fresh resubmission is treated as safer than guessing where
  processing left off.
- **Non-fatal embedding failures**: if alt text is generated successfully
  but the PDF can't be tagged (most commonly because the source PDF has no
  structure tree to splice into), the job still completes — the alt text
  itself is not thrown away, and the job carries a caveat message rather
  than being marked failed. Editors get partial value (raw alt text via the
  API) even when full embedding isn't possible for a given PDF.
- **Everything else** (extraction failure, generation failure, unexpected
  errors) marks the job `FAILED` with the error recorded on the job — it's
  never silently dropped.

## Data retention

- **FAILED jobs**: on-disk files (source PDF, any partial output) are
  deleted 24 hours after failure. The job record itself is untouched — it
  stays queryable via the API indefinitely.
- **COMPLETE jobs**: files are never automatically deleted.
- **Full reset** (`clean.py`) is a separate, manual, irreversible operation
  that wipes every job's files and database record and clears the Telegram
  board. It requires typing an exact confirmation phrase and warns (without
  refusing) if any job is still in progress.

## Status visibility

A single, continuously-edited Telegram message shows every job's live
status (not per-editor DMs) — it's edited in place on every state change
rather than reposted, so the channel doesn't fill with duplicate messages.
Completed jobs roll off the board automatically (7 calendar days for
success, 3 *working* days for failures) — this only affects what's
displayed, not the underlying record or files. Pod-termination failures are
posted as separate alert messages so they can't be missed inside a routine
board update.

## API surface (integration contract)

No authentication — internal network only, by confirmed design scope. The
intake API and the background worker run as two separate processes so a
slow/stuck job can never block a status request.

| Endpoint | Purpose |
|---|---|
| `POST /jobs` | Submit a PDF (multipart) with an editor id and optional passthrough reference |
| `GET /jobs/{id}` | One job's current state |
| `GET /jobs` | Paginated list, filterable by editor/state |
| `GET /jobs/{id}/results` | Alt-text rows, once `COMPLETE` |
| `GET /jobs/{id}/tagged-pdf` | The alt-text-embedded PDF, once available |
| `GET /health` | Liveness check |

Upload validation: rejects non-`.pdf` filenames, empty files, files over
100MB, and content that doesn't start with a valid PDF header — before
anything is written to disk or queued.

## Known gaps

- Alt-text re-embedding is not guaranteed for every PDF — coverage depends
  on how the source PDF is structured internally. Both a predicted and a
  measured coverage figure are recorded per job, and an under-tagged PDF is
  flagged rather than served as if complete — see
  [EMBEDDING_behaviour.md](EMBEDDING_behaviour.md).
- No retry logic — a `FAILED` job requires manual resubmission.
- No priority/expedite mechanism — strictly first-in-first-out.
