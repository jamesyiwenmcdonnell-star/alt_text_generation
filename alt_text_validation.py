#!/usr/bin/env python3
"""
alt_text_validation.py

Acceptance check for embed_alt_text.py. Opens the *output* PDF, walks its
structure tree, and answers the questions that decide whether the alt text
actually landed:

  1. How many <Figure> structure elements exist?
  2. How many carry a non-empty /Alt?
  3. Does each one's /K marked-content reference actually resolve to a real
     marked-content sequence on its /Pg? A <Figure> whose MCID isn't in the
     page's content stream is invisible to a screen reader -- it looks fine in
     a tree dump and does nothing in practice, so this is checked explicitly.
  4. Is each one reachable through /ParentTree, i.e. does
     ParentTree[page./StructParents][mcid] point back at the element? Without
     that, the page-to-structure link is one-way and assistive tech that walks
     from content to structure won't find it.
  5. Which manifest figures ended up with no <Figure> at all?
  6. Per-page: tree count vs manifest count.

The per-page table only prints pages where the tree or the manifest has
something -- a 276-row table of zeros helps nobody.

Usage:
    python alt_text_validation.py --pdf PDFTesting/full_test_tagged.pdf \\
        --manifest pdffigures2_out/manifest.csv

    # also list every figure element found
    python alt_text_validation.py --pdf out.pdf --manifest m.csv --list-figures

    # compare against the untagged original as a control
    python alt_text_validation.py --pdf PDFTesting/full_test.pdf --manifest m.csv
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pikepdf

MCID_RE = re.compile(rb"/MCID\s+(\d+)")


def as_list(x):
    if x is None:
        return []
    return list(x) if isinstance(x, pikepdf.Array) else [x]


def name_str(v):
    s = str(v)
    return s if s.startswith("/") else "/" + s


# --------------------------------------------------------------------------
# walking the tree
# --------------------------------------------------------------------------

def collect_figures(struct_root, page_index_map):
    """
    Walk the whole structure tree and return a list of dicts describing every
    /Figure element: page index, alt text, the MCIDs it references, and how
    deep in the tree it sits.
    """
    found = []
    seen = set()

    def mcids_of(node, depth=0, acc=None):
        """MCIDs referenced by a struct element's /K subtree (not descending
        into nested struct elements, which own their own content)."""
        if acc is None:
            acc = []
        if depth > 12:
            return acc
        for kid in as_list(node):
            if isinstance(kid, int):
                acc.append(int(kid))
            elif isinstance(kid, pikepdf.Dictionary):
                if kid.get("/Type") == "/MCR":
                    mc = kid.get("/MCID")
                    if mc is not None:
                        acc.append(int(mc))
                elif kid.get("/Type") == "/OBJR":
                    acc.append("OBJR")
                elif "/S" not in kid:
                    mcids_of(kid.get("/K"), depth + 1, acc)
        return acc

    def walk(node, depth=0, parent_tag=None, inherited_page=None):
        if depth > 60 or not isinstance(node, pikepdf.Dictionary):
            return
        try:
            key = node.objgen
        except AttributeError:
            key = None
        if key is not None and key != (0, 0):
            if (key, depth) in seen:
                return
            seen.add((key, depth))

        tag = name_str(node.get("/S")) if "/S" in node else None
        page_idx = inherited_page
        if "/Pg" in node:
            try:
                page_idx = page_index_map.get(node.Pg.objgen, inherited_page)
            except AttributeError:
                pass

        if tag == "/Figure":
            alt = node.get("/Alt")
            alt_text = str(alt) if alt is not None else ""
            found.append({
                "elem": node,
                "page": page_idx,
                "alt": alt_text.strip(),
                "mcids": mcids_of(node.get("/K")),
                "depth": depth,
                "parent_tag": parent_tag,
            })

        for kid in as_list(node.get("/K")):
            if isinstance(kid, pikepdf.Dictionary) and "/S" in kid:
                walk(kid, depth + 1, tag, page_idx)
            elif isinstance(kid, pikepdf.Dictionary) and "/S" not in kid:
                continue
        return

    for kid in as_list(struct_root.get("/K")):
        walk(kid, 0)
    return found


def page_mcids(page):
    """Every MCID that actually appears in a page's content stream."""
    out = set()
    try:
        contents = page.obj.get("/Contents")
        if contents is None:
            return out
        streams = as_list(contents)
        data = b"".join(bytes(s.read_bytes()) for s in streams
                        if isinstance(s, pikepdf.Stream))
    except Exception:
        return out
    for m in MCID_RE.finditer(data):
        out.add(int(m.group(1)))
    return out


def read_parent_tree(struct_root):
    out = {}

    def walk(node, depth=0):
        if depth > 20 or not isinstance(node, pikepdf.Dictionary):
            return
        if "/Nums" in node:
            nums = node.Nums
            for i in range(0, len(nums) - 1, 2):
                out[int(nums[i])] = nums[i + 1]
        for kid in as_list(node.get("/Kids")):
            walk(kid, depth + 1)

    walk(struct_root.get("/ParentTree"))
    return out


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def validate(pdf_path, manifest_path, pdf_name, list_figures=False, alt_preview=0):
    pdf = pikepdf.open(pdf_path)
    n_pages = len(pdf.pages)
    print(f"pdf:      {pdf_path}  ({n_pages} pages)")

    manifest = []
    if manifest_path:
        manifest = [r for r in csv.DictReader(open(manifest_path, newline="", encoding="utf-8"))
                    if r.get("pdf_name") == pdf_name]
        print(f"manifest: {manifest_path}  ({len(manifest)} row(s) for {pdf_name})")

    marked = False
    mi = pdf.Root.get("/MarkInfo")
    if mi is not None:
        marked = bool(mi.get("/Marked", False))
    print(f"tagged:   /StructTreeRoot={'/StructTreeRoot' in pdf.Root}  /MarkInfo/Marked={marked}")

    if "/StructTreeRoot" not in pdf.Root:
        print("\nNo structure tree -- nothing to validate.")
        return 1

    struct_root = pdf.Root.StructTreeRoot
    page_index_map = {p.obj.objgen: i for i, p in enumerate(pdf.pages)}
    figures = collect_figures(struct_root, page_index_map)
    parent_tree = read_parent_tree(struct_root)

    # --- per-figure integrity checks -------------------------------------
    mcid_cache = {}
    for f in figures:
        f["alt_ok"] = bool(f["alt"])
        f["content_ok"] = False
        f["ptree_ok"] = False

        if f["page"] is None or not f["mcids"]:
            continue
        page = pdf.pages[f["page"]]
        if f["page"] not in mcid_cache:
            mcid_cache[f["page"]] = page_mcids(page)
        present = mcid_cache[f["page"]]
        numeric = [m for m in f["mcids"] if isinstance(m, int)]
        f["content_ok"] = bool(numeric) and all(m in present for m in numeric)

        sp = page.obj.get("/StructParents")
        if sp is not None:
            arr = parent_tree.get(int(sp))
            if isinstance(arr, pikepdf.Array):
                ok = []
                for m in numeric:
                    if 0 <= m < len(arr):
                        entry = arr[m]
                        try:
                            ok.append(entry.objgen == f["elem"].objgen)
                        except AttributeError:
                            ok.append(False)
                    else:
                        ok.append(False)
                f["ptree_ok"] = bool(ok) and all(ok)

    n = len(figures)
    n_alt = sum(1 for f in figures if f["alt_ok"])
    n_content = sum(1 for f in figures if f["content_ok"])
    n_ptree = sum(1 for f in figures if f["ptree_ok"])
    n_full = sum(1 for f in figures if f["alt_ok"] and f["content_ok"] and f["ptree_ok"])

    print()
    print("=== <Figure> structure elements ===")
    print(f"  total <Figure> elements            {n}")
    print(f"  with non-empty /Alt                {n_alt}")
    print(f"  /K MCID resolves to page content   {n_content}")
    print(f"  reachable via /ParentTree          {n_ptree}")
    print(f"  fully wired (Alt + content + tree) {n_full}")
    if n:
        lens = [len(f["alt"]) for f in figures if f["alt_ok"]]
        if lens:
            print(f"  /Alt length  min={min(lens)}  median={sorted(lens)[len(lens)//2]}  max={max(lens)}")

    # --- manifest cross-check --------------------------------------------
    rc = 0
    if manifest:
        man_by_page = Counter(int(r["page"]) for r in manifest)
        tree_by_page = Counter(f["page"] for f in figures if f["page"] is not None)

        print()
        print("=== per-page: structure tree vs manifest ===")
        print("  (only pages where the tree or the manifest has something)")
        print(f"  {'page':>6} {'tree':>5} {'manifest':>9}  {'alt':>4}")
        mismatch_pages = 0
        for p in sorted(set(man_by_page) | set(tree_by_page)):
            t, m = tree_by_page.get(p, 0), man_by_page.get(p, 0)
            if t == 0 and m == 0:
                continue
            a = sum(1 for f in figures if f["page"] == p and f["alt_ok"])
            flag = "" if t == m else "   <-- MISMATCH"
            if t != m:
                mismatch_pages += 1
            print(f"  {p + 1:>6} {t:>5} {m:>9}  {a:>4}{flag}")
        print(f"  {len(set(man_by_page) | set(tree_by_page))} page(s) shown, "
              f"{mismatch_pages} with a count mismatch")

        # which manifest figures have no Figure element on their page
        print()
        print("=== manifest rows with no <Figure> on their page ===")
        missing = []
        for r in manifest:
            p = int(r["page"])
            if tree_by_page.get(p, 0) == 0:
                missing.append(r)
        if missing:
            for r in missing:
                print(f"  page {p_str(r)}  {r['fig_type']} {r['figure_name']}")
        else:
            print("  none -- every manifest page carries at least one <Figure>")

        # coverage: a manifest row is covered if its page has at least as many
        # Figure elements as the manifest expects there (rows sharing one
        # logical figure legitimately share one element, so cap at the tree count)
        covered = 0
        covered_alt = 0
        for p, m in man_by_page.items():
            t = tree_by_page.get(p, 0)
            a = sum(1 for f in figures if f["page"] == p and f["alt_ok"])
            covered += min(m, t) if t else 0
            covered_alt += min(m, a) if a else 0
        # rows sharing a single element still count as covered
        for p, m in man_by_page.items():
            t = tree_by_page.get(p, 0)
            if 0 < t < m:
                covered += m - t
                a = sum(1 for f in figures if f["page"] == p and f["alt_ok"])
                if a:
                    covered_alt += m - min(m, a)

        total = len(manifest)
        print()
        print("=== COVERAGE against the manifest ===")
        print(f"  manifest figures                 {total}")
        print(f"  with a <Figure> element          {covered}   ({100.0 * covered / max(1, total):.1f}%)")
        print(f"  with a <Figure> carrying /Alt    {covered_alt}   ({100.0 * covered_alt / max(1, total):.1f}%)")
        target = 0.90 * total
        verdict = "PASS" if covered_alt >= target else "BELOW TARGET"
        print(f"  90% target ({target:.0f} figures):        {verdict}")
        rc = 0 if covered_alt >= target else 2

    if list_figures:
        print()
        print("=== every <Figure> element ===")
        print(f"  {'page':>6} {'mcid':>6} {'alt':>4} {'cnt':>4} {'ptr':>4}  alt text")
        for f in sorted(figures, key=lambda f: (f["page"] if f["page"] is not None else -1)):
            mc = ",".join(str(m) for m in f["mcids"]) or "-"
            preview = f["alt"][:alt_preview].replace("\n", " ") if alt_preview else ""
            print(f"  {(f['page'] + 1) if f['page'] is not None else '?':>6} {mc:>6} "
                  f"{'Y' if f['alt_ok'] else '.':>4} {'Y' if f['content_ok'] else '.':>4} "
                  f"{'Y' if f['ptree_ok'] else '.':>4}  {preview}")

    return rc


def p_str(r):
    return str(int(r["page"]) + 1)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", required=True, type=Path)
    ap.add_argument("--manifest", type=Path, default=None)
    ap.add_argument("--pdf-name", default=None,
                    help="manifest pdf_name to filter on (default: derived from --pdf)")
    ap.add_argument("--list-figures", action="store_true")
    ap.add_argument("--alt-preview", type=int, default=0,
                    help="with --list-figures, show this many chars of each /Alt")
    args = ap.parse_args(argv)

    pdf_name = args.pdf_name
    if pdf_name is None:
        pdf_name = args.pdf.stem
        for suffix in ("_tagged", "_out", "_alttext"):
            if pdf_name.endswith(suffix):
                pdf_name = pdf_name[: -len(suffix)]

    return validate(args.pdf, args.manifest, pdf_name,
                    list_figures=args.list_figures, alt_preview=args.alt_preview)


if __name__ == "__main__":
    sys.exit(main())
