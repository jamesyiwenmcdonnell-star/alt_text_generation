#!/usr/bin/env python3
"""
pdf_batch_runner.py

File-management / orchestration layer around the pdffigures2 CLI jar
(org.allenai.pdffigures2.FigureExtractorBatchCli). Everything in this file is
plain Python (folder walking, subprocess, JSON/CSV) -- the only thing outside
this script is the actual `java -jar pdffigures2-assembly-*.jar` extraction
call, which pdffigures2_guide.md documents.

What it does:
  1. Recursively find PDFs under an input folder.
  2. Lay out a clean output directory (figures/, data/, stats/, logs/).
  3. Invoke the pdffigures2 CLI once per batch (it does its own multi-threading
     internally via -t), skipping PDFs that already have output if --skip-done.
  4. Parse the per-document JSON pdffigures2 writes and flatten every detected
     figure/table across every PDF into one manifest CSV, ready to hand to the
     Phase 2 model-eval harness as an image list.
  5. Validate figure/table numbering per PDF (e.g. 1.1, 1.2, 2.1, 2.2) for gaps
     -- a missing number (1.1, 1.3 with no 1.2) usually means pdffigures2
     missed a real figure, not that the paper skipped a number. Writes
     validation_report.csv.

Usage:
    python pdf_batch_runner.py \
        --input-dir /path/to/PDFTesting \
        --jar /path/to/pdffigures2-assembly-0.1.0.jar \
        --output-dir ./pdffigures2_out \
        --dpi 300 --threads 4
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# pdf_batch_runner.py itself lives at the project root, so these are fixed
# relative to its own location -- independent of the caller's cwd, unlike the
# old CLI-only --input-dir/--jar (which were required with no default).
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_DIR = SCRIPT_DIR / "PDFTesting"
DEFAULT_JAR_PATH = SCRIPT_DIR / "pdffigures2" / "pdffigures2.jar"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "pdffigures2_out"


# --------------------------------------------------------------------------- #
# File management
# --------------------------------------------------------------------------- #

def find_pdfs(input_dir: Path, recursive: bool = True) -> list[Path]:
    """Find all PDF files under input_dir. Case-insensitive on extension."""
    if not input_dir.is_dir():
        raise NotADirectoryError(f"{input_dir} is not a directory")

    pattern = "**/*" if recursive else "*"
    pdfs = sorted(
        p for p in input_dir.glob(pattern)
        if p.is_file() and p.suffix.lower() == ".pdf"
    )
    return pdfs


def make_output_layout(output_dir: Path) -> dict[str, Path]:
    """Create and return the output subdirectories pdffigures2 will write into."""
    layout = {
        "figures": output_dir / "figures",
        "data": output_dir / "data",
        "stats": output_dir / "stats",
        "logs": output_dir / "logs",
    }
    for path in layout.values():
        path.mkdir(parents=True, exist_ok=True)
    return layout


def already_processed(pdf_path: Path, data_dir: Path) -> bool:
    """A PDF counts as done if its per-doc figure JSON already exists."""
    expected = data_dir / f"{pdf_path.stem}.json"
    return expected.exists()


# --------------------------------------------------------------------------- #
# pdffigures2 invocation (the one part that calls out to the library's CLI)
# --------------------------------------------------------------------------- #

PDFFIGURES2_MAIN_CLASS = "org.allenai.pdffigures2.FigureExtractorBatchCli"


@dataclass
class ExtractionConfig:
    jar_path: Path
    output_dir: Path
    dpi: int = 300
    threads: int = 4
    image_format: str = "png"
    ignore_errors: bool = True
    java_bin: str = "java"
    extra_jvm_args: list[str] = field(default_factory=list)
    # Extra jars to put ahead of jar_path on the classpath, e.g. the JAI
    # ImageIO JPEG2000 plugin -- pdfbox/pdffigures2 silently skips any
    # JPEG2000-encoded image ("Cannot read JPEG2000 image: Java Advanced
    # Imaging (JAI) Image I/O Tools are not installed") without it, which on a
    # scan-heavy PDF can undercount figures by an order of magnitude. `-jar`
    # ignores -cp/CLASSPATH entirely (it only honors the jar's own manifest
    # Class-Path), so any extra jars force invoking by main class via -cp
    # instead of -jar.
    extra_classpath: list[str] = field(default_factory=list)


def run_pdffigures2(
    pdfs: list[Path], config: ExtractionConfig, logger: logging.Logger, batch_label: str = "batch"
) -> Path:
    """
    Invoke FigureExtractorBatchCli once over the given batch of PDFs.
    Returns the path to this batch's stats JSON file pdffigures2 wrote.

    Everything in `pdfs` runs inside ONE JVM process. pdffigures2 has no
    per-file crash isolation -- an uncaught OutOfMemoryError while rasterizing
    one PDF kills the whole process, taking down every other PDF still queued
    in that same invocation (their output simply never gets written). Keep
    `pdfs` small (see --batch-size in main()) so a bad PDF's blast radius is
    contained to its own batch, not your whole run.
    """
    layout = make_output_layout(config.output_dir)
    stats_file = layout["stats"] / f"stats_{batch_label}.json"

    # pdffigures2's CLI takes input as a SINGLE scopt `arg[Seq[String]]("<input>")`
    # positional argument -- not `.unbounded()` -- so scopt's default Seq[String]
    # reader expects one comma-separated token, not one argv entry per file.
    # Passing each PDF as its own argv element (the old behavior here) only let
    # the first one register; every PDF after it came back as "Unknown argument".
    offending = [str(p) for p in pdfs if "," in str(p)]
    if offending:
        raise ValueError(
            f"pdffigures2 joins input paths with commas; these filenames contain "
            f"a comma and would break the split: {offending}"
        )
    input_arg = ",".join(str(p) for p in pdfs)

    if config.extra_classpath:
        classpath = os.pathsep.join([str(config.jar_path), *config.extra_classpath])
        jar_or_classpath_args = ["-cp", classpath, PDFFIGURES2_MAIN_CLASS]
    else:
        jar_or_classpath_args = ["-jar", str(config.jar_path)]

    cmd = [
        config.java_bin,
        *config.extra_jvm_args,
        *jar_or_classpath_args,
        input_arg,
        "-i", str(config.dpi),
        "-m", str(layout["figures"]) + "/",
        "-d", str(layout["data"]) + "/",
        "-s", str(stats_file),
        "-f", config.image_format,
        "-t", str(config.threads),
    ]
    if config.ignore_errors:
        cmd.append("-e")

    logger.info("running pdffigures2 (%s) on %d PDFs: %s", batch_label, len(pdfs), [p.name for p in pdfs])
    logger.debug("command: %s", " ".join(cmd))

    log_file = layout["logs"] / f"{batch_label}.log"
    with open(log_file, "w") as fh:
        result = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT)

    if result.returncode != 0:
        logger.error(
            "pdffigures2 (%s) exited with code %d -- see %s for details. "
            "If this is an OutOfMemoryError, the PDFs in THIS batch may be "
            "missing output; re-run with --skip-done to retry just those, "
            "optionally with --batch-size 1 and/or a higher --java-heap.",
            batch_label, result.returncode, log_file,
        )
        if not config.ignore_errors:
            raise RuntimeError(f"pdffigures2 failed, see {log_file}")

    return stats_file


# --------------------------------------------------------------------------- #
# Manifest building (flattens per-doc JSON output into one CSV)
# --------------------------------------------------------------------------- #

MANIFEST_COLUMNS = [
    "pdf_name", "figure_name", "fig_type", "page",
    "caption", "image_path", "x1", "y1", "x2", "y2",
]


def build_manifest(data_dir: Path, figures_dir: Path, output_csv: Path, logger: logging.Logger) -> int:
    """
    Read every <pdf_stem>.json in data_dir and write one flat CSV row per
    detected figure/table, with the path to its rasterized image (if any)
    resolved against figures_dir. Returns the number of figures written.
    """
    rows = []
    doc_jsons = sorted(data_dir.glob("*.json"))

    for doc_json in doc_jsons:
        pdf_name = doc_json.stem
        try:
            figures = json.loads(doc_json.read_text())
        except json.JSONDecodeError as exc:
            logger.warning("skipping unparseable %s: %s", doc_json, exc)
            continue

        # pdffigures2 names saved images "<prefix><docName>-<figType><name>-<id>.<format>"
        # (confirmed against FigureExtractorBatchCli.scala's getFilenames: no hyphen
        # between figType and name, plus a mandatory 1-based <id> that only exceeds 1
        # when the same figType+name recurs in this PDF). Rather than reconstruct that
        # exactly (id-collision counting can drift from pdffigures2's own), glob-match
        # against what's actually on disk -- robust to the small formatting details.
        seen_counts: dict[tuple[str, str], int] = {}

        for fig in figures:
            fig_type = fig.get("figType", "")
            name = fig.get("name", "")
            boundary = fig.get("regionBoundary", {}) or {}

            key = (fig_type, name)
            seen_counts[key] = seen_counts.get(key, 0) + 1
            fig_id = seen_counts[key]

            exact = sorted(figures_dir.glob(f"{pdf_name}-{fig_type}{name}-{fig_id}.*"))
            loose = exact or sorted(figures_dir.glob(f"{pdf_name}-{fig_type}{name}-*.*"))
            image_path = loose[0] if loose else None
            if not loose:
                logger.debug("no saved image found for %s %s%s in %s", pdf_name, fig_type, name, figures_dir)

            rows.append({
                "pdf_name": pdf_name,
                "figure_name": name,
                "fig_type": fig_type,
                "page": fig.get("page", ""),
                "caption": (fig.get("caption") or "").replace("\n", " ").strip(),
                "image_path": str(image_path) if image_path else "",
                "x1": boundary.get("x1", ""),
                "y1": boundary.get("y1", ""),
                "x2": boundary.get("x2", ""),
                "y2": boundary.get("y2", ""),
            })

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(output_csv, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=MANIFEST_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    logger.info("manifest: %d figures across %d PDFs -> %s", len(rows), len(doc_jsons), output_csv)
    return len(rows)


# --------------------------------------------------------------------------- #
# Figure-numbering validation (detect pdffigures2 silently missing a figure)
# --------------------------------------------------------------------------- #

_NAME_PART_RE = re.compile(r"^\d+$")

VALIDATION_COLUMNS = ["pdf_name", "fig_type", "issue", "detail"]


def parse_figure_name(name: str) -> tuple[int, ...] | None:
    """
    Parse a pdffigures2 figure `name` like "1", "2.1", or "3.2.1" into a tuple
    of ints for gap-checking, e.g. "2.1" -> (2, 1). Returns None if any
    '.'-separated part isn't a plain non-negative integer (e.g. "1a", "S1",
    ""), since those can't be sequence-checked automatically -- they get
    reported as their own issue instead of silently skipped.
    """
    if not name:
        return None
    parts = name.split(".")
    if not all(_NAME_PART_RE.match(p) for p in parts):
        return None
    return tuple(int(p) for p in parts)


def _format_name(numbers: tuple[int, ...]) -> str:
    return ".".join(str(n) for n in numbers)


def _find_gaps(numbers: set[tuple[int, ...]]) -> list[tuple[int, ...]]:
    """
    Given a set of same-length int-tuples that share every element except the
    last (e.g. {(1,1), (1,2), (1,4)}), return the missing tuples for values
    1..max at that last position (e.g. [(1,3)]). Numbering is assumed to
    start at 1, matching how pdffigures2 (and papers generally) number
    figures -- there's no "figure 0" to anchor a lower bound otherwise.
    """
    if not numbers:
        return []
    prefix = next(iter(numbers))[:-1]
    present = {n[-1] for n in numbers}
    max_n = max(present)
    return [(*prefix, i) for i in range(1, max_n) if i not in present]


def validate_figure_sequences(data_dir: Path, logger: logging.Logger) -> list[dict]:
    """
    For each PDF's extracted figure JSON, check that figure/table `name`
    numbering has no gaps -- checked independently per (pdf, figType) since
    Figures and Tables are separate sequences in most papers. Two kinds of
    gaps are checked:
      - missing_major: a whole top-level number is absent, e.g. only 1.x and
        3.x exist -- figure "2" (or "2.x") may be missing entirely.
      - missing_minor: within one major group, e.g. 1.1, 1.3 present but not
        1.2.
    Names that aren't plain '.'-separated integers (e.g. "1a", "S1") are
    reported as unparsed_name rather than silently excluded, since they can't
    be gap-checked but may still be worth a look.

    Returns one dict per issue found (empty list = nothing suspicious).
    """
    issues: list[dict] = []
    doc_jsons = sorted(data_dir.glob("*.json"))

    for doc_json in doc_jsons:
        pdf_name = doc_json.stem
        try:
            figures = json.loads(doc_json.read_text())
        except json.JSONDecodeError as exc:
            logger.warning("skipping unparseable %s: %s", doc_json, exc)
            continue

        by_type: dict[str, list[tuple[int, ...]]] = {}
        for fig in figures:
            fig_type = fig.get("figType", "")
            name = fig.get("name", "")
            parsed = parse_figure_name(name)
            if parsed is None:
                issues.append({
                    "pdf_name": pdf_name,
                    "fig_type": fig_type,
                    "issue": "unparsed_name",
                    "detail": f"name={name!r} isn't a plain numeric ('.'-separated) "
                              f"name, couldn't check its sequence",
                })
            else:
                by_type.setdefault(fig_type, []).append(parsed)

        for fig_type, numbers in by_type.items():
            numbers_set = set(numbers)

            majors = {n[0] for n in numbers_set}
            major_gaps = _find_gaps({(m,) for m in majors})
            for (gap,) in major_gaps:
                issues.append({
                    "pdf_name": pdf_name,
                    "fig_type": fig_type,
                    "issue": "missing_major",
                    "detail": f"no {fig_type} numbered {gap} (or {gap}.x) found at all "
                              f"(have majors {sorted(majors)})",
                })

            prefixes = {n[:-1] for n in numbers_set if len(n) > 1}
            for prefix in prefixes:
                group = {n for n in numbers_set if n[:-1] == prefix}
                for gap in _find_gaps(group):
                    have = sorted(_format_name(n) for n in group)
                    issues.append({
                        "pdf_name": pdf_name,
                        "fig_type": fig_type,
                        "issue": "missing_minor",
                        "detail": f"gap in {_format_name(prefix)}.x sequence: "
                                  f"{_format_name(gap)} missing (have {have})",
                    })

    return issues


def write_validation_report(issues: list[dict], output_csv: Path, logger: logging.Logger) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(output_csv, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=VALIDATION_COLUMNS)
        writer.writeheader()
        writer.writerows(issues)

    if issues:
        by_issue: dict[str, int] = {}
        for i in issues:
            by_issue[i["issue"]] = by_issue.get(i["issue"], 0) + 1
        logger.warning(
            "validation: %d potential issue(s) across %d PDF(s) -> %s (%s)",
            len(issues), len({i["pdf_name"] for i in issues}), output_csv,
            ", ".join(f"{k}={v}" for k, v in sorted(by_issue.items())),
        )
    else:
        logger.info("validation: no numbering gaps or unparsed names found -> %s", output_csv)


# --------------------------------------------------------------------------- #
# CLI entry point
# --------------------------------------------------------------------------- #

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input-dir", default=DEFAULT_INPUT_DIR, type=Path,
                         help=f"Folder to search for PDFs (default: {DEFAULT_INPUT_DIR})")
    parser.add_argument("--jar", default=DEFAULT_JAR_PATH, type=Path,
                         help=f"Path to pdffigures2-assembly-*.jar (default: {DEFAULT_JAR_PATH})")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, type=Path)
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument(
        "--java-heap", default="4g",
        help="Max JVM heap for the pdffigures2 process, e.g. 4g, 8g (default: 4g). "
             "Rasterizing figures at high DPI across multiple PDFs in parallel "
             "(--threads) can OOM on the default JVM heap -- raise this before "
             "lowering --threads if you hit 'java.lang.OutOfMemoryError: Java heap space'.",
    )
    parser.add_argument(
        "--batch-size", type=int, default=1,
        help="How many PDFs to hand to a single `java` invocation at once "
             "(default: 1, one JVM per PDF). pdffigures2 has no per-file crash "
             "isolation -- an OutOfMemoryError while rendering one PDF kills the "
             "whole JVM, silently losing output for every other PDF queued in "
             "that same invocation. Raise this once you've confirmed your heap "
             "size handles it, to cut JVM-startup overhead on large batches.",
    )
    parser.add_argument("--no-recursive", action="store_true", help="Don't search subfolders")
    parser.add_argument("--skip-done", action="store_true", help="Skip PDFs already extracted")
    parser.add_argument(
        "--extra-classpath", default="",
        help=f"{os.pathsep}-separated jars to put on the classpath ahead of --jar, e.g. "
             "the JAI ImageIO JPEG2000 plugin jars. Without these, pdffigures2 silently "
             "skips every JPEG2000-encoded image ('Cannot read JPEG2000 image: Java "
             "Advanced Imaging (JAI) Image I/O Tools are not installed'), which can "
             "undercount figures badly on scan-heavy PDFs. Switches the java invocation "
             f"from `-jar` to `-cp ... {PDFFIGURES2_MAIN_CLASS}` since `-jar` ignores -cp. "
             "Defaults to $JAI_JPEG2000_CLASSPATH, which the container image sets.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def extract_images(
    input_dir: Path = DEFAULT_INPUT_DIR,
    jar_path: Path = DEFAULT_JAR_PATH,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    dpi: int = 300,
    threads: int = 4,
    java_heap: str = "4g",
    batch_size: int = 1,
    recursive: bool = True,
    skip_done: bool = False,
    extra_classpath: list[str] | None = None,
    logger: logging.Logger | None = None,
) -> dict:
    """Runs the full pdffigures2 extraction pipeline: finds PDFs under
    input_dir, invokes pdffigures2 batch-by-batch, and builds manifest.csv +
    validation_report.csv in output_dir. Callable directly (e.g. from
    controller.py) instead of only via the CLI below.

    Returns a dict with pdfs_found (total PDFs under input_dir), pdfs_processed
    (how many actually ran this call -- lower than pdfs_found when skip_done
    filters out already-done ones), manifest_path, validation_path, and
    figures_dir (the last three are None if no PDFs were found at all).
    """
    logger = logger or logging.getLogger("pdf_batch_runner")
    input_dir = Path(input_dir)
    jar_path = Path(jar_path)
    output_dir = Path(output_dir)

    # Default to the JAI ImageIO JPEG2000 jars the container image bakes in (see
    # docker/pdffigures2-build/Dockerfile), so in-process callers like
    # job_pipeline.py get JPEG2000 decoding without each having to wire the
    # classpath through. Unset off-container, where it correctly stays a no-op.
    if extra_classpath is None:
        extra_classpath = [p for p in os.environ.get("JAI_JPEG2000_CLASSPATH", "").split(os.pathsep) if p]
        if extra_classpath:
            logger.debug("using JAI_JPEG2000_CLASSPATH from environment: %s", extra_classpath)
        else:
            logger.warning(
                "JAI_JPEG2000_CLASSPATH not set and no extra_classpath given -- any "
                "JPEG2000-encoded images will be silently skipped by pdffigures2"
            )

    if not jar_path.exists():
        raise FileNotFoundError(f"jar not found: {jar_path} (build it with `sbt assembly`, see SETUP.md)")

    pdfs = find_pdfs(input_dir, recursive=recursive)
    pdfs_found = len(pdfs)
    if not pdfs:
        logger.warning("no PDFs found under %s", input_dir)
        return {"pdfs_found": 0, "pdfs_processed": 0, "manifest_path": None, "validation_path": None, "figures_dir": None}
    logger.info("found %d PDFs under %s", len(pdfs), input_dir)

    layout = make_output_layout(output_dir)

    if skip_done:
        before = len(pdfs)
        pdfs = [p for p in pdfs if not already_processed(p, layout["data"])]
        logger.info("skipping %d already-processed PDFs, %d remaining", before - len(pdfs), len(pdfs))

    if not pdfs:
        logger.info("nothing left to process")
    else:
        batch_size = max(1, batch_size)
        chunks = [pdfs[i:i + batch_size] for i in range(0, len(pdfs), batch_size)]
        logger.info(
            "processing %d PDFs in %d batch(es) of up to %d (one JVM invocation each)",
            len(pdfs), len(chunks), batch_size,
        )
        config = ExtractionConfig(
            jar_path=jar_path,
            output_dir=output_dir,
            dpi=dpi,
            threads=threads,
            extra_jvm_args=[f"-Xmx{java_heap}"],
            extra_classpath=extra_classpath or [],
        )
        failed_batches = 0
        for i, chunk in enumerate(chunks, start=1):
            label = f"batch{i:03d}"
            try:
                run_pdffigures2(chunk, config, logger, batch_label=label)
            except RuntimeError:
                # only reachable if ignore_errors=False; kept so a hard-failure
                # mode is available later without changing this loop's shape
                failed_batches += 1
                logger.error("batch %s raised, continuing with remaining batches", label)
        if failed_batches:
            logger.warning(
                "%d/%d batches failed -- re-run with skip_done=True to retry only "
                "the PDFs missing output", failed_batches, len(chunks),
            )

    manifest_path = output_dir / "manifest.csv"
    build_manifest(layout["data"], layout["figures"], manifest_path, logger)

    validation_path = output_dir / "validation_report.csv"
    issues = validate_figure_sequences(layout["data"], logger)
    write_validation_report(issues, validation_path, logger)

    return {
        "pdfs_found": pdfs_found,
        "pdfs_processed": len(pdfs),
        "manifest_path": manifest_path,
        "validation_path": validation_path,
        "figures_dir": layout["figures"],
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("pdf_batch_runner")

    try:
        extract_images(
            input_dir=args.input_dir,
            jar_path=args.jar,
            output_dir=args.output_dir,
            dpi=args.dpi,
            threads=args.threads,
            java_heap=args.java_heap,
            batch_size=args.batch_size,
            recursive=not args.no_recursive,
            skip_done=args.skip_done,
            # None (not []) when the flag is unset, so extract_images() falls
            # back to $JAI_JPEG2000_CLASSPATH rather than treating "no flag" as
            # "explicitly no extra jars"
            extra_classpath=[p for p in args.extra_classpath.split(os.pathsep) if p] or None,
            logger=logger,
        )
    except FileNotFoundError as exc:
        logger.error(str(exc))
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())