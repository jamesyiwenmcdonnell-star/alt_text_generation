# Embedding behaviour

How the `EMBEDDING` stage turns generated alt text into real, screen-reader-visible
structure — and, just as importantly, how it reports when it couldn't. This is the
deep reference for one pipeline stage; for the system as a whole see
[BEHAVIORS.md](BEHAVIORS.md), for day-to-day operation [COMMANDS.md](COMMANDS.md),
for first-time setup [SETUP.md](SETUP.md).

## The problem this stage solves

Alt text in a CSV helps nobody using a screen reader. To be announced, each
description has to become a `<Figure>` structure element that carries `/Alt` and
points — via a marked-content id — at the actual operators that paint the figure
on the page.

Source PDFs don't cooperate uniformly. Some arrive properly tagged with `<Figure>`
elements already wrapping their artwork. Some are nominally tagged but
semantically empty (a `/StructTreeRoot` holding only `/Document` and `/Part`, with
one marked-content id covering a whole page of text). Some have no structure tree
at all. The embedder handles the first two and declines the third — and the whole
point of the checks described below is that **it says which happened** instead of
producing a file that looks finished either way.

## Where the checks sit

```
QUEUED → EXTRACTING → EXTRACTED → POD_STARTING → GENERATING → EMBEDDING → COMPLETE
                          │                                        │
                     PRE-CHECK                                POST-CHECK
              predicts coverage, before                 measures real coverage,
                any GPU is provisioned                    after the PDF is written
```

Both checks answer the same question — *what fraction of this document's figures
can actually be tagged?* — at different times and with different authority. The
pre-check is a prediction; the post-check is a measurement.

## Pre-check (before generation)

`job_pipeline.precheck_embedding()` runs immediately after extraction, **before**
the GPU pod starts. It calls `embed_alt_text.preflight()`, which runs the real
matching logic in dry-run mode against the extracted manifest.

It can do this without any alt text because **matching is purely geometric** — it
compares pdffigures2's figure regions against bounding boxes computed from the
page's content stream. The alt text is only substituted at the very end, so
predicting coverage needs none of it. Running the actual `embed()` code path
(rather than a separate estimator) is deliberate: a parallel implementation would
drift from the real one and start lying.

The pre-check writes two things to the job and returns:

| Field | Meaning |
|---|---|
| `embed_precheck_coverage` | Predicted fraction of manifest figures that can be tagged (0.0–1.0) |
| `embed_note` | Human-readable verdict, prefixed `LOW EMBED CONFIDENCE` when below threshold |

**It is advisory — it never blocks the job.** A PDF predicted to embed badly still
goes on to generate alt text, because the alt-text CSV has standalone value even
when the tagged PDF can't be produced. What the pre-check buys is *timing*: an
un-embeddable document is visible on the status board minutes into the job,
while there's still a chance to cancel it, rather than after a full GPU run.

The pre-check also never raises. A show-stopper (no `/StructTreeRoot`, no manifest
rows for this document, an unreadable PDF) comes back as `ok=False` with the
reason recorded, and a pre-check that somehow crashes outright is logged and
swallowed — a broken *prediction* must not kill a job that might still produce
perfectly good alt text.

## How matching works

Coverage is decided by matching each manifest figure region to a **balanced unit**
of the content stream that can be wrapped without breaking anything. Three passes
run in order; each only sees the figures the previous passes left unmatched.

**Pass 0 — adopt existing `<Figure>` elements.** If the document already has real
`<Figure>` structure elements over its artwork, those only need `/Alt` set.
Creating a second `<Figure>` over the same content would be actively wrong.

**Pass 1 — `/EmbeddedDocument` marked-content blocks.** LaTeX's `\includegraphics`
of PDF artwork leaves each figure wrapped in a balanced `BDC…EMC` block, and those
line up with pdffigures2's regions almost 1:1. This is the highest-quality signal
available and the reason well-behaved documents reach 100%.

**Pass 2 — `BT…ET` text blocks.** The fallback for LaTeX-typeset tables, which
have no `/EmbeddedDocument` wrapper. Requires the block to sit almost entirely
inside the figure region, and refuses to wrap a span that would swallow a text
block belonging to something else.

**Pass 3 — bare XObject `Do` operators.** The last resort, for documents with no
`/EmbeddedDocument` blocks at all. Wrapping a `Do` is the safest splice in the
script — it's a single operator, so `/Figure BDC` + `Do` + `EMC` is balanced by
construction, with no operator-range surgery and no `q…Q` nesting risk. It is also
the pass most able to do damage, which is why it is gated twice (below).

Every splice wraps an **already-balanced** unit, so `BDC`/`EMC` nesting and the
`q`/`Q` graphics stack stay valid by construction. New marked-content ids are
allocated above the page's existing maximum, so they can never collide with ids
the document already uses.

### The two gates on pass 3

An XObject can be an `/Image` (a leaf — nothing inside it to conflict with, safe to
wrap) or a `/Form` (a *container* that can hold text, nested forms, its own marked
content, its own `/StructParent`). The danger is a form that covers the whole page:
wrapping one would make an entire page a single `<Figure>` with one alt text —
worse than no tagging, and completely silent about it.

**Gate 1 — IoU scoring.** Candidates are scored by intersection-over-union, not by
intersection over the smaller area. Under a min-area score a whole-page form scores
a perfect 1.000 against any region inside it; under IoU an oversized candidate
scores *worse* the bigger it is. True matches sit at 0.99+; the floor is 0.50.

**Gate 2 — `classify_do_target()` safety classifier.** Returns `wrap` or
`fallback`, declining on any of: unresolvable resource, not an image or form,
`/StructParent` already present (it participates in the structure tree), missing
`/BBox` on a form (the geometry behind the match was meaningless), drawn bbox more
than 1.5× the figure region, unparseable content stream, or the form carrying its
own `/MCID` marked content (which would collide with the parent tree).

Nested forms are scanned recursively at bounded depth, and the classifier is
**closed by default** on anything it cannot see: if a nested `Do` can't be
resolved, or any content stream in the subtree won't parse, the whole form is
declined rather than assumed safe. An unexamined XObject is not an empty one —
treating "couldn't look" as "nothing there" is exactly how a whole-page form slips
through.

Text *inside* a form is not disqualifying — chart axis labels and legends are
legitimately part of the figure. Size is the reliable signal.

A `wrap` verdict is a **safety** judgement — the splice won't damage the document —
not a **correctness** one. That the alt text describes the right artwork is what
the IoU score approximates, and neither gate can fully guarantee it.

## Post-check (after embedding)

Once the tagged PDF is written, `summarize_report()` reduces the per-row report to
the numbers the pipeline acts on. Each manifest row carries an outcome status;
`tagged-*`, `shared-*` and `existing-*` mean the figure ended up as (or inside) a
real `<Figure>` element, and everything else (`no-match`, `scan-failed`,
`page-out-of-range`) means it didn't.

Coverage is `tagged / total`. Below **`EMBED_MIN_COVERAGE` (0.90)** the job is
flagged. The threshold is set where it is because documents whose structure the
embedder genuinely understands sit at 99–100% on the test corpus — anything
materially below that means whole figures will be silent for a screen-reader user.

The post-check writes:

| Field | Meaning |
|---|---|
| `embed_coverage` | Measured fraction actually tagged. **`NULL` until the post-check runs** |
| `embed_note` | Supersedes the pre-check's note; same `LOW EMBED CONFIDENCE` prefix when flagged |
| `error_message` | Set only when flagged, so a caveat rides on the `COMPLETE` state |

`embed_coverage` being `NULL` is what distinguishes a *prediction* from a
*measurement*, and consumers rely on that distinction — see the filename rule
below.

## What each outcome looks like to an editor

Embedding is non-fatal in every case: the job reaches `COMPLETE` and the alt-text
CSV stays downloadable regardless, because a PDF that can't be tagged shouldn't
throw away alt text that was generated successfully.

**Full coverage.** `error_message` is null, `embed_note` reads `embedded: N/N
figures tagged (100%)`, the tagged PDF downloads under its normal name.

**Low coverage (embedding worked, but under threshold).** A tagged PDF exists and
is served, but it is incomplete. `embed_coverage` holds the real fraction,
`embed_note` and `error_message` both say so, the status board shows a `⚠` line,
and `GET /jobs/{id}/tagged-pdf` serves the file under the download filename
**`PARTIALLY TAGGED PDF - <stem>_tagged.pdf`**. The on-disk name is untouched;
only the `Content-Disposition` header changes. This exists so a consumer that
checks nothing but `state == COMPLETE` still cannot mistake a partial file for a
complete one.

The rename is gated on `embed_coverage` being set **and** the note carrying the
low-confidence prefix — so only the measured post-check verdict can trigger it. A
pre-check prediction of low confidence never renames a file, because at that point
nothing has actually been embedded yet.

**Embedding failed outright** (no `/StructTreeRoot`, no manifest rows, unreadable
PDF). No tagged PDF is produced. `error_message` explains why, `embed_coverage`
stays `NULL`, and `GET /jobs/{id}/tagged-pdf` returns `404` carrying that reason.
The alt text is still available from `GET /jobs/{id}/results`.

A note on failure containment: `embed_alt_text.embed()` signals its failure paths
with `SystemExit`, which is correct for a standalone CLI but is a `BaseException` —
an `except Exception` would not catch it, and it would unwind all the way out
through the worker's polling loop and **kill the daemon** over one bad PDF. It's
converted to a `RuntimeError` at the call site, with `except (Exception,
SystemExit)` around the stage as defence in depth. `KeyboardInterrupt` deliberately
still propagates, so Ctrl-C stops the worker promptly.

## Tuning parameters

All in `embed_alt_text.py` except the threshold, which is in `job_pipeline.py`.

| Parameter | Value | What it controls |
|---|---|---|
| `EMBED_MIN_COVERAGE` | 0.90 | Below this fraction, a job is flagged low-confidence |
| `MIN_OVERLAP` | 0.30 | Region-to-unit overlap needed for a pass 1 match |
| `MIN_CONTAINMENT` | 0.70 | How much of a text block must sit inside the region (pass 2) |
| `XOBJ_MIN_IOU` | 0.50 | IoU floor for an XObject candidate (pass 3) |
| `XOBJ_MAX_SIZE_RATIO` | 1.5 | Max drawn-bbox-to-region area ratio before a form is declined |

`XOBJ_MAX_SIZE_RATIO` is the one most likely to need adjustment on a new corpus —
it's tuned on a small number of documents from two publishers. Loosening it
increases coverage and increases the risk of over-capture; tightening it does the
reverse.

## Verifying a tagged PDF

Alt text lives in the structure tree, not the rendered page — **no ordinary viewer
shows it, including Preview.app.**

- **Command line:** `alt_text_validation.py --pdf <tagged> --alt-preview 5`
- **Dry run, no write:** `embed_alt_text.py --pdf <pdf> --manifest <manifest> --dry-run`
  prints the same per-row outcomes and coverage the pipeline would record
- **macOS:** Acrobat Pro (`View → Show/Hide → Navigation Panes → Tags`); free
  Acrobat Reader's `View → Read Out Loud` will speak it
- **Windows:** PAC 2024 (free, best dedicated tool); NVDA for a real screen-reader test

Everything runs inside the `pdffigures2-builder` container — pikepdf is not
installed on the host:

```bash
docker run --rm -v "$(pwd):/work" -w /work --entrypoint python3 pdffigures2-builder embed_alt_text.py --pdf PDFTesting/<file>.pdf --manifest pdffigures2_out/manifest.csv --fallback-caption --dry-run
```

The manifest must be the one produced for that PDF, and rows are joined on the
manifest's `pdf_name` column, which defaults to the PDF's filename stem. If you've
renamed the file since extraction, pass `--pdf-name <original-stem>` or every row
will miss and you'll get `no manifest rows for pdf_name=...` — which is the same
message the pre-check reports as an `ok=False` verdict.

## Known limits

- **A `wrap` verdict is a safety judgement, not a correctness one.** Nothing here
  verifies that a given alt text describes the artwork it got attached to.
- **The size threshold is corpus-tuned.** Two publishers' worth of documents is a
  thin basis for a geometric constant.
- **Blank-crop descriptions pass through unfiltered.** pdffigures2 sometimes crops
  whitespace, and the model dutifully describes it ("The image is entirely blank
  with no discernible content"). A screen reader will announce that verbatim.
- **Coverage counts structure, not quality.** A figure tagged with a poor
  description counts toward coverage exactly like a good one.
- **The filename warning is the only signal in the file itself.** Nothing is
  written into the PDF to mark it as partially tagged; a renamed download that
  gets renamed again loses the warning.
