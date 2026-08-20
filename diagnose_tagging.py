#!/usr/bin/env python3
"""
diagnose_tagging.py

Answers the one question that determines how hard pikepdf-based alt-text
re-embedding will actually be for a given PDF: is it already tagged, and if
so, do its <Figure>/<Table> structure elements roughly line up with what
pdffigures2 detected?

For each PDF it reports:
  - is_tagged: does /Root/StructTreeRoot exist at all (untagged = you'd be
    building a structure tree from scratch, a much bigger job).
  - marked_flag: /Root/MarkInfo/Marked -- a second, sometimes-inconsistent
    signal of tagging intent; reported alongside is_tagged, not instead of it.
  - struct_figures / struct_tables: how many <Figure>/<Table> structure
    elements exist in the tag tree.
  - struct_figures_missing_content_link: of those, how many don't resolve to
    any actual page content (MCID or object reference) -- a structure
    element with no content link is a dead end for bbox-based matching.
  - struct_figures_with_alt: how many already have non-empty /Alt set.
  - pdffigures2_figures / pdffigures2_tables (only if --pdffigures2-data is
    given): counts from that PDF's <name>.json, for direct comparison.
  - verdict: a plain-English read on which of the two difficulty tiers this
    PDF falls into, per the counts above.

This does NOT do bounding-box matching or write any alt text -- it only
tells you what you're dealing with before building that part.

Usage:
    python diagnose_tagging.py --input-dir ./PDFTesting
    python diagnose_tagging.py --input-dir ./PDFTesting \
        --pdffigures2-data ./pdffigures2_out/data --output-csv tagging_report.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pikepdf

REPORT_COLUMNS = [
    "pdf_name", "is_tagged", "marked_flag",
    "struct_figures", "struct_tables",
    "struct_figures_missing_content_link", "struct_figures_with_alt",
    "pdffigures2_figures", "pdffigures2_tables",
    "verdict",
]

ELEMENT_COLUMNS = ["pdf_name", "struct_type", "page_index", "has_alt", "has_content_link"]

_CONTENT_REF_TYPES = {"/MCR", "/OBJR"}


@dataclass
class StructElementInfo:
    struct_type: str
    page_index: int | None
    has_alt: bool
    has_content_link: bool


def _name_str(value) -> str:
    """pikepdf.Name reprs include the leading '/'; normalize for comparison."""
    s = str(value)
    return s if s.startswith("/") else f"/{s}"


def _resolve_page_index(pg_ref, page_index_by_objgen: dict) -> int | None:
    if pg_ref is None:
        return None
    try:
        return page_index_by_objgen.get(pg_ref.objgen)
    except AttributeError:
        return None


def _has_content_link(node, depth: int = 0) -> bool:
    """
    Bounded search of a structure element's /K subtree for anything that
    actually points at page content: a bare MCID (int), an /MCR (marked
    content reference) dict, or an /OBJR (object reference, e.g. straight at
    an image XObject) dict. A structure element whose /K only contains other
    structure elements (no leaf content ref anywhere below it) is a dead end
    for matching against pdffigures2's page content.
    """
    if depth > 12:  # guard against any malformed/cyclic tree
        return False
    if isinstance(node, int):
        return True
    if isinstance(node, pikepdf.Array):
        return any(_has_content_link(child, depth + 1) for child in node)
    if isinstance(node, pikepdf.Dictionary):
        type_name = _name_str(node.get("/Type")) if "/Type" in node else None
        if type_name in _CONTENT_REF_TYPES:
            return True
        if "/K" in node:
            return _has_content_link(node.K, depth + 1)
        return False
    return False


def _walk_struct_tree(
    node,
    page_index_by_objgen: dict,
    out: list[StructElementInfo],
    inherited_page: int | None = None,
    depth: int = 0,
) -> None:
    """Recursively collect Figure/Table structure elements from a /K subtree."""
    if depth > 50:  # guard against malformed/cyclic trees
        return

    if isinstance(node, pikepdf.Array):
        for child in node:
            _walk_struct_tree(child, page_index_by_objgen, out, inherited_page, depth + 1)
        return

    if not isinstance(node, pikepdf.Dictionary):
        return  # bare int (MCID) or something else -- not a structure element

    type_name = _name_str(node.get("/Type")) if "/Type" in node else None
    if type_name in _CONTENT_REF_TYPES:
        return  # a content leaf, not a structure element -- nothing to record

    own_page = _resolve_page_index(node.get("/Pg"), page_index_by_objgen)
    page_index = own_page if own_page is not None else inherited_page

    struct_type = _name_str(node.get("/S")) if "/S" in node else None
    if struct_type in ("/Figure", "/Table"):
        alt = node.get("/Alt")
        out.append(StructElementInfo(
            struct_type=struct_type.lstrip("/"),
            page_index=page_index,
            has_alt=bool(alt) and str(alt).strip() != "",
            has_content_link=_has_content_link(node.get("/K"), depth) if "/K" in node else False,
        ))

    if "/K" in node:
        _walk_struct_tree(node.K, page_index_by_objgen, out, page_index, depth + 1)


def diagnose_pdf(pdf_path: Path, logger: logging.Logger) -> tuple[dict, list[StructElementInfo]]:
    """Open one PDF and return (summary dict without pdffigures2/verdict fields, element list)."""
    with pikepdf.open(pdf_path) as pdf:
        root = pdf.Root
        is_tagged = "/StructTreeRoot" in root

        mark_info = root.get("/MarkInfo")
        marked_flag = bool(mark_info is not None and mark_info.get("/Marked", False))

        elements: list[StructElementInfo] = []
        if is_tagged:
            page_index_by_objgen = {p.obj.objgen: i for i, p in enumerate(pdf.pages)}
            struct_root = root.StructTreeRoot
            if "/K" in struct_root:
                _walk_struct_tree(struct_root.K, page_index_by_objgen, elements)

        summary = {
            "pdf_name": pdf_path.stem,
            "is_tagged": is_tagged,
            "marked_flag": marked_flag,
            "struct_figures": sum(1 for e in elements if e.struct_type == "Figure"),
            "struct_tables": sum(1 for e in elements if e.struct_type == "Table"),
            "struct_figures_missing_content_link": sum(
                1 for e in elements if e.struct_type == "Figure" and not e.has_content_link
            ),
            "struct_figures_with_alt": sum(
                1 for e in elements if e.struct_type == "Figure" and e.has_alt
            ),
        }
        return summary, elements


def _load_pdffigures2_counts(data_dir: Path, pdf_stem: str, logger: logging.Logger) -> tuple[int | None, int | None]:
    json_path = data_dir / f"{pdf_stem}.json"
    if not json_path.exists():
        return None, None
    try:
        figures = json.loads(json_path.read_text())
    except json.JSONDecodeError as exc:
        logger.warning("couldn't parse %s: %s", json_path, exc)
        return None, None
    n_figures = sum(1 for f in figures if f.get("figType") == "Figure")
    n_tables = sum(1 for f in figures if f.get("figType") == "Table")
    return n_figures, n_tables


def _verdict(row: dict) -> str:
    if not row["is_tagged"]:
        return "UNTAGGED -- no structure tree to attach to; building one from scratch is a much bigger task than alt-text injection alone"

    if row["struct_figures"] == 0 and row["struct_tables"] == 0:
        return "TAGGED but no Figure/Table structure elements -- infra (StructTreeRoot) exists, but figures/tables aren't tagged; you'd be adding new tags into an existing tree, not building one from nothing"

    pf_figs = row["pdffigures2_figures"]
    pf_tabs = row["pdffigures2_tables"]
    if pf_figs is None:
        note = " (no pdffigures2 data supplied to compare against)"
        fig_match = tab_match = None
    else:
        fig_match = row["struct_figures"] == pf_figs
        tab_match = row["struct_tables"] == pf_tabs
        note = ""

    if row["struct_figures_missing_content_link"] > 0:
        return (
            f"TAGGED, {row['struct_figures']} Figure element(s), but "
            f"{row['struct_figures_missing_content_link']} have no resolvable "
            f"page content link -- those are dead ends for bbox matching, worth checking by hand{note}"
        )

    if fig_match is False or tab_match is False:
        return (
            f"TAGGED but counts differ from pdffigures2 (struct: {row['struct_figures']} figures/"
            f"{row['struct_tables']} tables vs pdffigures2: {pf_figs} figures/{pf_tabs} tables) -- "
            f"needs real matching logic, 1:1 positional assumption won't hold"
        )

    if fig_match and tab_match:
        return "TAGGED, counts match pdffigures2 exactly -- good candidate for straightforward matching + /Alt injection"

    return f"TAGGED, {row['struct_figures']} Figure / {row['struct_tables']} Table element(s) found{note}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input-dir", required=True, type=Path, help="Folder of PDFs to check")
    parser.add_argument(
        "--pdffigures2-data", type=Path, default=None,
        help="Path to a pdffigures2_out/data/ dir (per-PDF JSON) to compare struct-tree "
             "counts against. Optional -- omit to just check tagging status.",
    )
    parser.add_argument("--output-csv", type=Path, default=Path("./tagging_report.csv"))
    parser.add_argument(
        "--dump-elements", action="store_true",
        help="Also write tagging_elements.csv with one row per Figure/Table structure "
             "element found (page, has_alt, has_content_link) -- useful once you're past "
             "the summary and into deciding how matching should work.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("diagnose_tagging")

    pdfs = sorted(p for p in args.input_dir.glob("*.pdf") if p.is_file())
    if not pdfs:
        logger.warning("no PDFs found under %s", args.input_dir)
        return 0

    rows = []
    all_elements: list[tuple[str, StructElementInfo]] = []

    for pdf_path in pdfs:
        try:
            summary, elements = diagnose_pdf(pdf_path, logger)
        except pikepdf.PdfError as exc:
            logger.error("couldn't open %s: %s", pdf_path, exc)
            rows.append({
                "pdf_name": pdf_path.stem, "is_tagged": "", "marked_flag": "",
                "struct_figures": "", "struct_tables": "",
                "struct_figures_missing_content_link": "", "struct_figures_with_alt": "",
                "pdffigures2_figures": "", "pdffigures2_tables": "",
                "verdict": f"ERROR opening PDF: {exc}",
            })
            continue

        if args.pdffigures2_data:
            pf_figs, pf_tabs = _load_pdffigures2_counts(args.pdffigures2_data, pdf_path.stem, logger)
        else:
            pf_figs, pf_tabs = None, None
        summary["pdffigures2_figures"] = pf_figs
        summary["pdffigures2_tables"] = pf_tabs
        summary["verdict"] = _verdict(summary)
        rows.append(summary)
        all_elements.extend((pdf_path.stem, e) for e in elements)

        logger.info("%s: %s", pdf_path.name, summary["verdict"])

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output_csv, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=REPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    logger.info("wrote %s (%d PDFs)", args.output_csv, len(rows))

    if args.dump_elements:
        elements_csv = args.output_csv.parent / "tagging_elements.csv"
        with open(elements_csv, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=ELEMENT_COLUMNS)
            writer.writeheader()
            for pdf_name, e in all_elements:
                writer.writerow({
                    "pdf_name": pdf_name, "struct_type": e.struct_type,
                    "page_index": e.page_index, "has_alt": e.has_alt,
                    "has_content_link": e.has_content_link,
                })
        logger.info("wrote %s (%d elements)", elements_csv, len(all_elements))

    n_tagged = sum(1 for r in rows if r["is_tagged"] is True)
    logger.info("summary: %d/%d PDFs tagged", n_tagged, len(rows))

    return 0


if __name__ == "__main__":
    sys.exit(main())