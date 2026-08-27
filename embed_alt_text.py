#!/usr/bin/env python3
"""
embed_alt_text.py

Embeds AI-generated alt text into a PDF as real, screen-reader-visible
structure: one <Figure> structure element per detected figure, each carrying
/Alt, /Pg and a /K marked-content reference that actually points at the pixels
on the page.

Why this is not just "set /Alt on the existing Figure tags":
    full_test.pdf is nominally tagged (it has a /StructTreeRoot) but the tree is
    semantically empty -- only /Document and /Part, zero /Figure, zero /MCR,
    zero /OBJR, no /RoleMap. Every leaf /Part holds a single MCID covering a
    whole page of text. There is nothing to attach alt text *to*, so the
    structure has to be created.

Why this is not just "add /StructParent to the image XObjects":
    only 12 of the 68 manifest figures are image XObjects at all. The other 82%
    are LaTeX-included vector/text graphics with no XObject to hang a
    /StructParent on. That route caps out at ~18% coverage.

How it actually works:
    The figures in this document are already delimited in the content stream.
    LaTeX's \\includegraphics of PDF artwork leaves each figure wrapped in a
    balanced `/EmbeddedDocument ... BDC ... EMC` marked-content block. Those
    blocks line up with pdffigures2's region boundaries almost 1:1 (71 blocks
    vs 68 manifest rows). So instead of guessing at operator ranges and CTM
    bookkeeping, we:

      1. scan each page's content stream, tracking the CTM and text matrix, to
         get a device-space bbox for every operator;
      2. collect the *balanced* units on the page -- outermost
         /EmbeddedDocument MC blocks, plus BT..ET text blocks that sit outside
         them (this is the fallback that picks up LaTeX-typeset tables, which
         have no EmbeddedDocument wrapper);
      3. match each manifest figure region (converted from pdffigures2's
         top-left-origin coords to PDF's bottom-left-origin) to the best-
         overlapping unit;
      4. splice `/Figure <</MCID k>> BDC` immediately before the unit and `EMC`
         immediately after it. Because we wrap an already-balanced unit, the
         BDC/EMC nesting and the q/Q stack stay valid by construction -- no
         operator-range surgery, no risk of splitting a q..Q block;
      5. create a /Figure StructElem carrying /Alt, /Pg and /K = that MCID,
         insert it into the existing tree under the chapter-level /Part that
         covers that page (so reading order stays roughly sane), and register
         it in /ParentTree under the page's /StructParents key.

    New MCIDs are allocated above the page's existing maximum so they can never
    collide with the 255 MCIDs already in the file.

    /ParentTree is rewritten as a single flat /Nums number tree rather than the
    original 8-node /Kids form. A number-tree root is allowed to hold /Nums
    directly, and at 276 entries there is no reason to maintain /Limits splits.

Alt text source:
    --alt-csv is joined to the manifest on (pdf_name, figure_name, fig_type,
    page). Rows whose alt_text is empty, or that have no matching alt-text row
    at all, fall back to the manifest caption if --fallback-caption is given,
    and are otherwise tagged as <Figure> with no /Alt (still an improvement --
    the figure becomes a real structure element -- but reported as uncovered).

NEVER writes to the input PDF. --output must be a different path.

Usage:
    python embed_alt_text.py \
        --pdf PDFTesting/full_test.pdf \
        --manifest pdffigures2_out/manifest.csv \
        --alt-csv jobs/<job>/alt_text_results.csv \
        --output PDFTesting/full_test_tagged.pdf

    # structure-only dry run, using captions as placeholder alt text
    python embed_alt_text.py --pdf PDFTesting/full_test.pdf \
        --manifest pdffigures2_out/manifest.csv \
        --fallback-caption --output PDFTesting/full_test_tagged.pdf
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from functools import partial
from pathlib import Path

import pikepdf

# A unit has to overlap the manifest region by at least this fraction of the
# smaller of the two areas before we'll call it a match. 0.30 is deliberately
# loose: pdffigures2's region boundary and the actual ink rarely agree closely,
# and there is normally only one candidate per region anyway.
MIN_OVERLAP = 0.30

# For the BT..ET fallback we want the opposite -- lots of small text blocks on
# the page, so require the block to sit almost entirely inside the region.
MIN_CONTAINMENT = 0.70

# XObject `Do` candidates are scored by IoU, not intersection/min(area): a form
# XObject can cover the whole page, and under a min-area score it would match
# any region inside it with a perfect 1.000 -- wrapping it would turn the whole
# page into one <Figure>. IoU makes an oversized candidate score *worse* the
# bigger it is. Observed true matches sit at 0.99+, so 0.50 is generous.
XOBJ_MIN_IOU = 0.50

# Belt-and-braces on top of the IoU score: a form whose drawn bbox area is more
# than this multiple of the figure region's area is declined outright by
# classify_do_target(). Tuned on a handful of documents from one publisher --
# this is the parameter most likely to need adjustment on new corpora.
XOBJ_MAX_SIZE_RATIO = 1.5

MC_FIGURE_TAGS = ("/EmbeddedDocument",)


# --------------------------------------------------------------------------
# content stream scanning
# --------------------------------------------------------------------------

def mat_mul(a, b):
    a0, a1, a2, a3, a4, a5 = a
    b0, b1, b2, b3, b4, b5 = b
    return (a0 * b0 + a1 * b2, a0 * b1 + a1 * b3,
            a2 * b0 + a3 * b2, a2 * b1 + a3 * b3,
            a4 * b0 + a5 * b2 + b4, a4 * b1 + a5 * b3 + b5)


def apply_mat(m, x, y):
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def _f(v):
    try:
        return float(v)
    except Exception:
        return 0.0


def scan_page(page):
    """
    Parse a page's content stream and return (ops, scanned) where scanned is a
    list of (index, operator_str, operands, device_bbox_or_None).

    The bbox is approximate: text is reduced to its pen origin plus a crude
    width estimate (we have no font metrics here), paths to their control
    points, XObjects to their /BBox or unit square under the CTM. That is
    plenty to decide which pdffigures2 region an operator belongs to.
    """
    ops = list(pikepdf.parse_content_stream(page))
    ctm = (1, 0, 0, 1, 0, 0)
    stack = []
    tm = tlm = (1, 0, 0, 1, 0, 0)
    tfs, tl, th, ts = 1.0, 0.0, 1.0, 0.0
    out = []

    for i, o in enumerate(ops):
        op = str(o.operator)
        a = list(o.operands)
        pts = []

        if op == "q":
            stack.append(ctm)
        elif op == "Q":
            if stack:
                ctm = stack.pop()
        elif op == "cm" and len(a) == 6:
            ctm = mat_mul(tuple(_f(x) for x in a), ctm)
        elif op == "BT":
            tm = tlm = (1, 0, 0, 1, 0, 0)
        elif op == "Tf" and len(a) >= 2:
            tfs = _f(a[1])
        elif op == "TL":
            tl = _f(a[0])
        elif op == "Tz":
            th = _f(a[0]) / 100.0
        elif op == "Ts":
            ts = _f(a[0])
        elif op == "Tm" and len(a) == 6:
            tm = tlm = tuple(_f(x) for x in a)
        elif op in ("Td", "TD") and len(a) == 2:
            if op == "TD":
                tl = -_f(a[1])
            tlm = mat_mul((1, 0, 0, 1, _f(a[0]), _f(a[1])), tlm)
            tm = tlm
        elif op == "T*":
            tlm = mat_mul((1, 0, 0, 1, 0, -tl), tlm)
            tm = tlm
        elif op in ("Tj", "TJ", "'", '"'):
            if op in ("'", '"'):
                tlm = mat_mul((1, 0, 0, 1, 0, -tl), tlm)
                tm = tlm
            trm = mat_mul(mat_mul((tfs * th, 0, 0, tfs, 0, ts), tm), ctm)
            n = 0
            try:
                if op == "Tj" and a:
                    n = len(bytes(a[0]))
                elif op == "TJ" and a:
                    n = sum(len(bytes(e)) for e in a[0] if isinstance(e, pikepdf.String))
                elif a:
                    n = len(bytes(a[-1]))
            except Exception:
                n = 0
            # 0.5 em per glyph is a rough but serviceable average advance
            pts = [apply_mat(trm, 0, 0), apply_mat(trm, 0, 1), apply_mat(trm, 0.5 * n, 0)]
        elif op in ("m", "l") and len(a) >= 2:
            pts = [apply_mat(ctm, _f(a[0]), _f(a[1]))]
        elif op in ("c", "v", "y") and len(a) >= 4:
            pts = [apply_mat(ctm, _f(a[j]), _f(a[j + 1])) for j in range(0, len(a) - 1, 2)]
        elif op == "re" and len(a) == 4:
            x, y, w, h = (_f(v) for v in a)
            pts = [apply_mat(ctm, x, y), apply_mat(ctm, x + w, y),
                   apply_mat(ctm, x, y + h), apply_mat(ctm, x + w, y + h)]
        elif op == "Do" and a:
            pts = _xobject_points(page, a[0], ctm)
        elif op == "INLINE_IMAGE":
            pts = [apply_mat(ctm, 0, 0), apply_mat(ctm, 1, 0),
                   apply_mat(ctm, 0, 1), apply_mat(ctm, 1, 1)]

        bbox = None
        if pts:
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            bbox = (min(xs), min(ys), max(xs), max(ys))
        out.append((i, op, a, bbox))

    return ops, out


def _xobject_points(page, name, ctm):
    try:
        xo = page.Resources.XObject[name]
        if xo.get("/Subtype") == "/Form" and "/BBox" in xo:
            bx = [_f(v) for v in xo.BBox]
            mtx = tuple(_f(v) for v in xo.Matrix) if "/Matrix" in xo else (1, 0, 0, 1, 0, 0)
            m = mat_mul(mtx, ctm)
            return [apply_mat(m, bx[0], bx[1]), apply_mat(m, bx[2], bx[1]),
                    apply_mat(m, bx[0], bx[3]), apply_mat(m, bx[2], bx[3])]
    except Exception:
        pass
    # image XObjects (and anything we couldn't resolve) live in the unit square
    return [apply_mat(ctm, 0, 0), apply_mat(ctm, 1, 0),
            apply_mat(ctm, 0, 1), apply_mat(ctm, 1, 1)]


# --------------------------------------------------------------------------
# XObject safety classification
# --------------------------------------------------------------------------

def _form_content_stats(xo, depth=0, seen=None):
    """
    Scan a form XObject's content stream (and nested forms, bounded depth) for
    the features that make wrapping its `Do` unsafe. Returns a dict, or None if
    any content stream in the subtree can't be parsed -- unparseable content
    means we can't vouch for what we'd be wrapping.
    """
    if seen is None:
        seen = set()
    key = getattr(xo, "objgen", None)
    if key is not None and key != (0, 0):  # (0,0) = direct object, not a usable identity
        if key in seen:
            return {"mcid": False, "struct_parent": False}
        seen.add(key)

    try:
        ops = list(pikepdf.parse_content_stream(xo))
    except Exception:
        return None

    stats = {"mcid": False, "struct_parent": False}
    for o in ops:
        op = str(o.operator)
        a = list(o.operands)
        if op == "BDC" and len(a) > 1 and isinstance(a[1], pikepdf.Dictionary):
            # A bare tag (LaTeX leaves e.g. `/Document BDC` with no /MCID) is
            # harmless -- no /MCID means no reference into any parent tree, so
            # nothing we add can collide with it. Only /MCID-bearing marked
            # content ties the form to existing structure.
            if "/MCID" in a[1]:
                stats["mcid"] = True
        elif op == "Do" and a and depth < 3:
            try:
                nested = xo.Resources.XObject[a[0]]
            except Exception:
                # can't see what this Do draws, so we can't vouch for the form
                # -- same contract as an unparseable content stream
                return None
            if "/StructParent" in nested:
                stats["struct_parent"] = True
            if str(nested.get("/Subtype", "")) == "/Form":
                sub = _form_content_stats(nested, depth + 1, seen)
                if sub is None:
                    return None
                stats["mcid"] = stats["mcid"] or sub["mcid"]
                stats["struct_parent"] = stats["struct_parent"] or sub["struct_parent"]
    return stats


def classify_do_target(page, name, region, bbox):
    """
    Decide whether wrapping this `Do` operator in /Figure BDC..EMC is safe.
    Returns (verdict, kind, reason) with verdict "wrap" or "fallback"; a
    fallback verdict means the figure stays with whatever the text /
    /EmbeddedDocument passes could do (usually nothing -- that's the point:
    better untagged and reported than wrongly tagged and silent).

    The asymmetry between image and form matters: an /Image is a leaf -- there
    is nothing inside it to conflict with, so it's safe to wrap almost
    unconditionally. A /Form is a container that can hold text, nested forms,
    its own marked content and its own /StructParent, each of which can make
    the wrap wrong. "wrap" is a *safety* judgement (the splice won't damage the
    document's structure), not a correctness one (that the alt text describes
    the right artwork) -- correctness rides on the IoU score in match_units().
    """
    try:
        xo = page.Resources.XObject[name]
    except Exception:
        return "fallback", "unresolved", "couldn't resolve the XObject resource"

    subtype = str(xo.get("/Subtype", ""))
    if subtype not in ("/Image", "/Form"):
        return "fallback", subtype or "unknown", "not an image or form XObject"

    if "/StructParent" in xo or "/StructParents" in xo:
        return "fallback", subtype[1:].lower(), \
            "XObject already participates in the structure tree (/StructParent)"

    # Over-capture guard: a candidate much larger than the figure region would
    # make one alt text speak for content that isn't the figure.
    if bbox is not None and bbox_area(bbox) > XOBJ_MAX_SIZE_RATIO * max(1e-6, bbox_area(region)):
        return "fallback", subtype[1:].lower(), \
            f"drawn bbox is >{XOBJ_MAX_SIZE_RATIO:g}x the figure region -- would over-capture"

    if subtype == "/Image":
        return "wrap", "image", "leaf image XObject"

    if "/BBox" not in xo:
        # without /BBox, _xobject_points() fell back to the unit square, so the
        # geometry that produced the match score was meaningless for a form
        return "fallback", "form", "form has no /BBox -- geometry untrustworthy"

    stats = _form_content_stats(xo)
    if stats is None:
        return "fallback", "form", "couldn't parse the form's content stream"
    if stats["struct_parent"]:
        return "fallback", "form", "a nested XObject carries /StructParent"
    if stats["mcid"]:
        return "fallback", "form", "form has its own /MCID marked content -- would collide with the parent tree"

    return "wrap", "form", "self-contained form XObject"


def union_bbox(boxes):
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def bbox_area(b):
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def bbox_intersection_area(a, b):
    dx = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    dy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return dx * dy


# --------------------------------------------------------------------------
# balanced units: the things we are allowed to wrap
# --------------------------------------------------------------------------

def marked_content_blocks(scanned):
    """All balanced BDC/BMC..EMC blocks as (start, end, tag, depth, mcid)."""
    stack = []
    blocks = []
    for i, op, a, _ in scanned:
        if op in ("BDC", "BMC"):
            tag = str(a[0]) if a else "?"
            mcid = None
            if len(a) > 1 and isinstance(a[1], pikepdf.Dictionary):
                mcid = a[1].get("/MCID")
            stack.append((i, tag, mcid))
        elif op == "EMC" and stack:
            s, tag, mcid = stack.pop()
            blocks.append((s, i, tag, len(stack), None if mcid is None else int(mcid)))
    return blocks


def text_blocks(scanned):
    """All balanced BT..ET blocks as (start, end)."""
    start = None
    out = []
    for i, op, _a, _b in scanned:
        if op == "BT":
            start = i
        elif op == "ET" and start is not None:
            out.append((start, i))
            start = None
    return out


def mcid_bboxes(scanned, blocks):
    """{mcid: device bbox} for every marked-content sequence carrying an MCID."""
    out = {}
    for s, e, _tag, _d, mcid in blocks:
        if mcid is None:
            continue
        bb = union_bbox(b for (i, _o, _a, b) in scanned if s <= i <= e)
        if bb is None:
            continue
        prev = out.get(mcid)
        out[mcid] = bb if prev is None else union_bbox([prev, bb])
    return out


def page_units(page):
    """
    Return (scanned, units, max_mcid) for one page.

    `units` is the list of wrappable candidates, each a dict with:
        start, end   -- operator index range, inclusive, balanced
        bbox         -- device-space bbox of everything inside
        kind         -- "mc" (an /EmbeddedDocument block), "text" (BT..ET), or
                        "xobj" (a single XObject `Do`; also carries "name")

    "mc" units are preferred; "text" units are only ever used as a fallback for
    figures that no mc unit covers (LaTeX-typeset tables, mainly); "xobj" units
    are the last resort, for documents with no /EmbeddedDocument blocks at all
    -- and only after classify_do_target() rules the wrap safe.
    """
    _ops, scanned = scan_page(page)
    blocks = marked_content_blocks(scanned)

    embedded = [b for b in blocks if b[2] in MC_FIGURE_TAGS]
    # keep only outermost -- /EmbeddedDocument sometimes nests /Document inside
    embedded = [b for b in embedded
                if not any(o[0] < b[0] and b[1] < o[1] for o in embedded)]

    units = []
    for s, e, _tag, _d, _mc in embedded:
        units.append({
            "start": s, "end": e, "kind": "mc",
            "bbox": union_bbox(bb for (i, _o, _a, bb) in scanned if s <= i <= e),
        })

    for s, e in text_blocks(scanned):
        if any(u["start"] < s and e < u["end"] for u in units):
            continue  # already inside an /EmbeddedDocument block
        units.append({
            "start": s, "end": e, "kind": "text",
            "bbox": union_bbox(bb for (i, _o, _a, bb) in scanned if s <= i <= e),
        })

    # Bare `Do` operators. Wrapping one is the safest splice available -- a
    # single operator, so /Figure BDC + Do + EMC is balanced by construction.
    # Skip any Do inside an /EmbeddedDocument unit (the mc unit already covers
    # it). A Do inside some *other* MCID-bearing block is NOT skipped: these
    # documents commonly wrap a whole page in one /Part <</MCID 0>> block, and
    # nesting a /Figure sequence inside it is exactly what the BT..ET text
    # path has always done (verified readable in Acrobat) -- excluding Do ops
    # on that ground would leave such pages permanently untaggable.
    for i, op, a, bb in scanned:
        if op != "Do" or not a or bb is None:
            continue
        if any(u["kind"] == "mc" and u["start"] < i < u["end"] for u in units):
            continue
        units.append({
            "start": i, "end": i, "kind": "xobj",
            "bbox": bb, "name": a[0],
        })

    max_mcid = -1
    for _s, _e, _t, _d, mcid in blocks:
        if mcid is not None and mcid > max_mcid:
            max_mcid = mcid

    return scanned, units, max_mcid, blocks


def existing_figure_boxes(page, scanned, blocks, parent_entries):
    """
    Some PDFs (unlike full_test.pdf) arrive already properly tagged, with real
    <Figure> structure elements wrapping their artwork -- those only need /Alt
    setting, and creating a second <Figure> over the same content would be
    actively wrong.

    Returns [(struct_elem, bbox)] for every <Figure> element that owns marked
    content on this page, located via the page's /StructParents entry.
    """
    sp = page.get("/StructParents")
    if sp is None:
        return []
    arr = parent_entries.get(int(sp))
    if not isinstance(arr, pikepdf.Array):
        return []

    boxes = mcid_bboxes(scanned, blocks)
    by_elem = {}
    for mcid, entry in enumerate(arr):
        if not isinstance(entry, pikepdf.Dictionary):
            continue
        if str(entry.get("/S")) != "/Figure":
            continue
        bb = boxes.get(mcid)
        if bb is None:
            continue
        key = entry.objgen
        if key in by_elem:
            by_elem[key] = (entry, union_bbox([by_elem[key][1], bb]))
        else:
            by_elem[key] = (entry, bb)
    return list(by_elem.values())


# --------------------------------------------------------------------------
# matching manifest regions to units
# --------------------------------------------------------------------------

def match_units(figures, units, classifier=None):
    """
    figures: list of dicts with a "region" key (device-space bbox).
    Returns {figure_index: (start, end, kind, score)} for whatever matched.

    Pass 1 takes the best /EmbeddedDocument block per figure. Pass 2 sweeps up
    anything left over using contiguous runs of BT..ET blocks contained in the
    region. Pass 3 tries bare XObject `Do` operators for figures still
    unmatched, gated by IoU and the `classifier` callback (see
    classify_do_target) -- without a classifier, pass 3 is skipped entirely.
    """
    matches = {}
    claimed = {}  # unit index -> figure key it was claimed for

    mc_units = [(i, u) for i, u in enumerate(units) if u["kind"] == "mc"]

    scored = []
    for fi, fig in enumerate(figures):
        reg = fig["region"]
        for ui, u in mc_units:
            bb = u["bbox"]
            if bb is None:
                continue
            if bbox_area(bb) <= 1.0:
                # degenerate (e.g. a single column of text): fall back to
                # whether the block's centre sits inside the region
                cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
                score = 1.0 if (reg[0] - 20 <= cx <= reg[2] + 20
                                and reg[1] - 20 <= cy <= reg[3] + 20) else 0.0
            else:
                denom = max(1e-6, min(bbox_area(reg), bbox_area(bb)))
                score = bbox_intersection_area(reg, bb) / denom
            if score >= MIN_OVERLAP:
                scored.append((score, fi, ui))

    # greedy best-first, but let two manifest rows share a unit when they are
    # the same logical figure -- pdffigures2 sometimes emits one figure as two
    # region boxes with an identical figure_name.
    for score, fi, ui in sorted(scored, key=lambda t: -t[0]):
        if fi in matches:
            continue
        key = figures[fi]["key"]
        if ui in claimed and claimed[ui] != key:
            continue
        claimed[ui] = key
        u = units[ui]
        matches[fi] = (u["start"], u["end"], u["kind"], score)

    # pass 2: BT..ET fallback for figures nothing covered
    text_units = sorted(((i, u) for i, u in enumerate(units) if u["kind"] == "text"),
                        key=lambda t: t[1]["start"])
    for fi, fig in enumerate(figures):
        if fi in matches:
            continue
        reg = fig["region"]
        inside = []
        for ui, u in text_units:
            bb = u["bbox"]
            if bb is None or ui in claimed:
                continue
            a = bbox_area(bb)
            if a <= 0:
                continue
            if bbox_intersection_area(reg, bb) / a >= MIN_CONTAINMENT:
                inside.append((ui, u))
        if not inside:
            continue
        start = min(u["start"] for _ui, u in inside)
        end = max(u["end"] for _ui, u in inside)
        # the wrapped span must not swallow any block that is *not* ours
        intruder = any(
            (u["start"] >= start and u["end"] <= end) and ui not in {x for x, _ in inside}
            for ui, u in text_units
        )
        if intruder:
            continue
        for ui, _u in inside:
            claimed[ui] = fig["key"]
        matches[fi] = (start, end, "text", 1.0)

    # pass 3: bare XObject `Do` fallback, IoU-scored (see XOBJ_MIN_IOU for why
    # not intersection/min-area) and safety-gated by the classifier. Candidates
    # are tried best-first so a declined (e.g. oversized) form doesn't shadow a
    # slightly worse but safe one.
    if classifier is not None:
        xobj_units = [(i, u) for i, u in enumerate(units) if u["kind"] == "xobj"]
        scored_x = []
        for fi, fig in enumerate(figures):
            if fi in matches:
                continue
            reg = fig["region"]
            for ui, u in xobj_units:
                bb = u["bbox"]
                if bb is None:
                    continue
                inter = bbox_intersection_area(reg, bb)
                union = bbox_area(reg) + bbox_area(bb) - inter
                iou = inter / union if union > 1e-6 else 0.0
                if iou >= XOBJ_MIN_IOU:
                    scored_x.append((iou, fi, ui))
        # global best-first (same shape as pass 1), so a unit two figures both
        # clear the IoU gate for goes to the better-scoring figure, not just
        # the one with the lower manifest index
        for iou, fi, ui in sorted(scored_x, key=lambda t: -t[0]):
            if fi in matches:
                continue
            key = figures[fi]["key"]
            if ui in claimed and claimed[ui] != key:
                continue
            u = units[ui]
            verdict, _kind, _reason = classifier(u["name"], figures[fi]["region"], u["bbox"])
            if verdict != "wrap":
                continue
            claimed[ui] = key
            matches[fi] = (u["start"], u["end"], "xobj", iou)

    return matches


# --------------------------------------------------------------------------
# structure tree plumbing
# --------------------------------------------------------------------------

def as_list(x):
    if x is None:
        return []
    return list(x) if isinstance(x, pikepdf.Array) else [x]


def read_parent_tree(struct_root):
    """Flatten the /ParentTree number tree (root /Nums or /Kids) to {key: value}."""
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


def write_parent_tree(pdf, struct_root, entries):
    """Rewrite /ParentTree as a single flat /Nums number tree."""
    nums = pikepdf.Array()
    for key in sorted(entries):
        nums.append(key)
        nums.append(entries[key])
    struct_root.ParentTree = pdf.make_indirect(pikepdf.Dictionary(Nums=nums))


def chapter_index(pdf, struct_root):
    """
    Map each chapter-level /Part (depth 1) to the set of page indices it covers,
    so new /Figure elements land in a sane spot in the reading order.

    Returns (document_element, [(part_element, min_page, max_page), ...]).
    """
    page_index = {p.obj.objgen: i for i, p in enumerate(pdf.pages)}

    def pages_under(node, depth=0, acc=None):
        if acc is None:
            acc = []
        if depth > 12 or not isinstance(node, pikepdf.Dictionary):
            return acc
        if "/Pg" in node:
            idx = page_index.get(node.Pg.objgen)
            if idx is not None:
                acc.append(idx)
        for kid in as_list(node.get("/K")):
            if isinstance(kid, pikepdf.Dictionary):
                pages_under(kid, depth + 1, acc)
        return acc

    top = as_list(struct_root.get("/K"))
    doc = top[0] if top else None
    if doc is None:
        return None, []

    chapters = []
    for part in as_list(doc.get("/K")):
        if not isinstance(part, pikepdf.Dictionary):
            continue
        pgs = pages_under(part)
        if pgs:
            chapters.append((part, min(pgs), max(pgs)))
    return doc, chapters


def insert_into_tree(pdf, doc, chapters, fig_elem, page_idx, page_index_map):
    """
    Insert fig_elem under the chapter /Part covering page_idx, positioned among
    that chapter's kids by page order. Falls back to appending to /Document.
    """
    target = None
    for part, lo, hi in chapters:
        if lo <= page_idx <= hi:
            target = part
            break
    if target is None:
        target = doc

    kids = target.get("/K")
    if not isinstance(kids, pikepdf.Array):
        kids = pikepdf.Array(as_list(kids))
        target.K = kids

    def kid_page(kid):
        if isinstance(kid, pikepdf.Dictionary) and "/Pg" in kid:
            return page_index_map.get(kid.Pg.objgen)
        return None

    pos = len(kids)
    for i, kid in enumerate(kids):
        kp = kid_page(kid)
        if kp is not None and kp > page_idx:
            pos = i
            break

    kids.insert(pos, fig_elem)
    fig_elem.P = target
    return target


# --------------------------------------------------------------------------
# content stream rewriting
# --------------------------------------------------------------------------

def rewrite_page(pdf, page, ops, insertions):
    """
    insertions: list of (start_index, end_index, mcid). Emits
    `/Figure <</MCID mcid>> BDC` before start_index and `EMC` after end_index.
    """
    before = defaultdict(list)
    after = defaultdict(list)
    for start, end, mcid in insertions:
        before[start].append(mcid)
        after[end].append(mcid)

    bdc = pikepdf.Operator("BDC")
    emc = pikepdf.Operator("EMC")
    fig = pikepdf.Name("/Figure")

    out = []
    for i, o in enumerate(ops):
        for mcid in before[i]:
            out.append(([fig, pikepdf.Dictionary(MCID=mcid)], bdc))
        out.append((o.operands, o.operator))
        # innermost first on the way out, so nesting stays balanced
        for mcid in reversed(after[i]):
            out.append(([], emc))

    page.Contents = pdf.make_stream(pikepdf.unparse_content_stream(out))


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

def load_manifest(path, pdf_name):
    rows = [r for r in csv.DictReader(open(path, newline="", encoding="utf-8"))
            if r.get("pdf_name") == pdf_name]
    return rows


def load_alt_text(paths):
    """{(pdf_name, figure_name, fig_type, page): alt_text} across one or more CSVs."""
    alt = {}
    for path in paths:
        try:
            rows = list(csv.DictReader(open(path, newline="", encoding="utf-8")))
        except OSError as exc:
            print(f"  warning: couldn't read {path}: {exc}")
            continue
        n = 0
        for r in rows:
            text = (r.get("alt_text") or "").strip()
            if not text:
                continue
            key = (r.get("pdf_name"), r.get("figure_name"), r.get("fig_type"), str(r.get("page")))
            alt[key] = text
            n += 1
        print(f"  loaded {n} alt-text row(s) from {path}")
    return alt


def embed(pdf_path, manifest_path, alt_paths, out_path, pdf_name,
          fallback_caption=False, dry_run=False):
    """dry_run=True runs the exact same matching/decision logic but never
    writes anything -- no content-stream rewrite, no save. The in-memory pdf
    object is still mutated (structure elements, /Alt on adopted figures) and
    then simply discarded, which keeps the dry run on the same code path as a
    real embed so its predicted report can't drift from reality."""
    if not dry_run and Path(out_path).resolve() == Path(pdf_path).resolve():
        raise SystemExit("refusing to overwrite the input PDF -- pick a different --output")

    rows = load_manifest(manifest_path, pdf_name)
    if not rows:
        raise SystemExit(f"no manifest rows for pdf_name={pdf_name!r} in {manifest_path}")
    print(f"manifest: {len(rows)} row(s) for {pdf_name}")

    alt_lookup = load_alt_text(alt_paths) if alt_paths else {}

    pdf = pikepdf.open(pdf_path)
    n_pages = len(pdf.pages)
    print(f"pdf: {n_pages} pages")

    if "/StructTreeRoot" not in pdf.Root:
        raise SystemExit("PDF has no /StructTreeRoot -- building a tree from scratch "
                         "is out of scope for this script")
    struct_root = pdf.Root.StructTreeRoot

    page_index_map = {p.obj.objgen: i for i, p in enumerate(pdf.pages)}
    parent_entries = read_parent_tree(struct_root)
    next_key = int(struct_root.get("/ParentTreeNextKey", max(parent_entries, default=-1) + 1))
    doc, chapters = chapter_index(pdf, struct_root)
    if doc is None:
        raise SystemExit("/StructTreeRoot has no /K -- nothing to splice into")

    # group manifest rows by page
    by_page = defaultdict(list)
    for r in rows:
        by_page[int(r["page"])].append(r)

    report = []  # one entry per manifest row
    n_elems = 0

    for page_idx in sorted(by_page):
        if page_idx >= n_pages:
            for r in by_page[page_idx]:
                report.append({"row": r, "status": "page-out-of-range", "mcid": None})
            continue

        page = pdf.pages[page_idx]
        height = float(page.MediaBox[3])

        figures = []
        for r in by_page[page_idx]:
            x1, y1, x2, y2 = (float(r[k]) for k in ("x1", "y1", "x2", "y2"))
            # pdffigures2 emits top-left-origin, y-down points; PDF is y-up
            figures.append({
                "row": r,
                "region": (x1, height - y2, x2, height - y1),
                "key": (r["fig_type"], r["figure_name"]),
            })

        try:
            scanned, units, max_mcid, blocks = page_units(page)
        except Exception as exc:
            print(f"  page {page_idx + 1}: scan failed ({exc})")
            for fig in figures:
                report.append({"row": fig["row"], "status": "scan-failed", "mcid": None})
            continue

        def alt_for(row):
            t = alt_lookup.get((row["pdf_name"], row["figure_name"],
                                row["fig_type"], str(row["page"])), "")
            if not t and fallback_caption:
                t = (row.get("caption") or "").strip()
            return t

        # --- pass 0: adopt <Figure> elements the document already has -------
        existing = existing_figure_boxes(page, scanned, blocks, parent_entries)
        pending = []
        if existing:
            claimed_elems = set()
            for fig in figures:
                reg = fig["region"]
                best, best_score = None, 0.0
                for ei, (elem, bb) in enumerate(existing):
                    if ei in claimed_elems:
                        continue
                    denom = max(1e-6, min(bbox_area(reg), bbox_area(bb)))
                    score = bbox_intersection_area(reg, bb) / denom
                    if score > best_score:
                        best_score, best = score, ei
                if best is not None and best_score >= MIN_OVERLAP:
                    claimed_elems.add(best)
                    elem = existing[best][0]
                    text = alt_for(fig["row"])
                    if text:
                        elem.Alt = pikepdf.String(text)
                    report.append({
                        "row": fig["row"],
                        "status": "existing-figure" + ("" if text else "-noalt"),
                        "mcid": None,
                    })
                else:
                    pending.append(fig)
        else:
            pending = list(figures)

        if not pending:
            continue
        figures = pending

        matches = match_units(figures, units,
                              classifier=partial(classify_do_target, page))
        # every pending figure gets a report row -- unmatched ones included,
        # so coverage denominators stay equal to the manifest row count
        for fi, fig in enumerate(figures):
            if fi not in matches:
                report.append({"row": fig["row"], "status": "no-match", "mcid": None})
        if not matches:
            continue

        sp_key = page.get("/StructParents")
        if sp_key is None:
            sp_key = next_key
            next_key += 1
            page.StructParents = sp_key
            parent_entries[sp_key] = pdf.make_indirect(pikepdf.Array())
        else:
            sp_key = int(sp_key)
        arr = parent_entries.get(sp_key)
        if not isinstance(arr, pikepdf.Array):
            arr = pikepdf.Array([arr] if arr is not None else [])
            parent_entries[sp_key] = pdf.make_indirect(arr)

        next_mcid = max(max_mcid + 1, len(arr))

        # one struct element per distinct unit (two manifest rows may share one)
        by_span = {}
        insertions = []
        for fi, (start, end, kind, score) in sorted(matches.items(),
                                                    key=lambda kv: kv[1][0]):
            row = figures[fi]["row"]
            span = (start, end)
            if span in by_span:
                mcid = by_span[span]
                report.append({"row": row, "status": f"shared-{kind}", "mcid": mcid})
                continue

            mcid = next_mcid
            next_mcid += 1
            by_span[span] = mcid

            text = alt_for(row)

            fig_elem = pdf.make_indirect(pikepdf.Dictionary(
                Type=pikepdf.Name("/StructElem"),
                S=pikepdf.Name("/Figure"),
                Pg=page.obj,
                K=mcid,
            ))
            if text:
                fig_elem.Alt = pikepdf.String(text)

            insert_into_tree(pdf, doc, chapters, fig_elem, page_idx, page_index_map)

            while len(arr) <= mcid:
                arr.append(None)
            arr[mcid] = fig_elem

            insertions.append((start, end, mcid))
            n_elems += 1
            report.append({
                "row": row,
                "status": f"tagged-{kind}" + ("" if text else "-noalt"),
                "mcid": mcid,
            })

        if insertions and not dry_run:
            ops = list(pikepdf.parse_content_stream(page))
            rewrite_page(pdf, page, ops, insertions)

    if dry_run:
        print(f"\ndry run: {n_elems} <Figure> element(s) would be created -- nothing written")
        return report

    write_parent_tree(pdf, struct_root, parent_entries)
    struct_root.ParentTreeNextKey = next_key

    # a tagged PDF should say so
    if "/MarkInfo" not in pdf.Root:
        pdf.Root.MarkInfo = pikepdf.Dictionary(Marked=True)
    else:
        pdf.Root.MarkInfo.Marked = True

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    pdf.save(out_path)
    print(f"\nwrote {out_path}  ({n_elems} <Figure> element(s) created)")
    return report


# Status prefixes that mean "this manifest row ended up as (or inside) a real
# <Figure> structure element". Shared with job_pipeline.py's coverage check.
OK_STATUS_PREFIXES = ("tagged", "shared", "existing")


def summarize_report(report):
    """Reduce a per-row report from embed() to the numbers callers act on."""
    counts = defaultdict(int)
    for e in report:
        counts[e["status"]] += 1
    total = len(report)
    tagged = sum(v for k, v in counts.items() if k.startswith(OK_STATUS_PREFIXES))
    with_alt = sum(v for k, v in counts.items()
                   if k.startswith(OK_STATUS_PREFIXES) and not k.endswith("noalt"))
    return {
        "total": total,
        "tagged": tagged,
        "with_alt": with_alt,
        "coverage": tagged / total if total else 0.0,
        "counts": dict(counts),
        "missed": [e for e in report if not e["status"].startswith(OK_STATUS_PREFIXES)],
    }


def preflight(pdf_path, manifest_path, pdf_name):
    """
    Predict embedding coverage for a PDF *before* any alt text exists, without
    writing anything. Runs the real matching logic (embed() in dry_run mode)
    so the prediction can't diverge from what embedding will actually do.

    Never raises: structural show-stoppers (no /StructTreeRoot, no manifest
    rows, unreadable PDF) come back as ok=False with the reason, so the
    pipeline can record "not confident enough to embed" instead of crashing.

    Returns {"ok": bool, "coverage": float, "tagged": int, "total": int,
             "reason": str | None}.
    """
    try:
        report = embed(pdf_path, manifest_path, alt_paths=[], out_path=None,
                       pdf_name=pdf_name, dry_run=True)
    except (Exception, SystemExit) as exc:
        return {"ok": False, "coverage": 0.0, "tagged": 0, "total": 0,
                "reason": str(exc) or exc.__class__.__name__}
    s = summarize_report(report)
    return {"ok": True, "coverage": s["coverage"], "tagged": s["tagged"],
            "total": s["total"], "reason": None}


def print_report(report):
    s = summarize_report(report)

    print("\n--- per-row outcome ---")
    for status in sorted(s["counts"]):
        print(f"  {status:24s} {s['counts'][status]:4d}")

    total = max(1, s["total"])
    print(f"\n  manifest rows:           {s['total']}")
    print(f"  rows given a <Figure>:   {s['tagged']}  ({100.0 * s['tagged'] / total:.1f}%)")
    print(f"  rows with non-empty Alt: {s['with_alt']}  ({100.0 * s['with_alt'] / total:.1f}%)")

    if s["missed"]:
        print("\n  not tagged:")
        for e in s["missed"]:
            r = e["row"]
            print(f"    page {int(r['page']) + 1:4d}  {r['fig_type']} {r['figure_name']}  ({e['status']})")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", required=True, type=Path)
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--alt-csv", type=Path, action="append", default=[],
                    help="alt_text_results.csv (repeatable)")
    ap.add_argument("--output", type=Path, default=None,
                    help="where to write the tagged PDF (required unless --dry-run)")
    ap.add_argument("--pdf-name", default=None,
                    help="manifest pdf_name to filter on (default: --pdf stem)")
    ap.add_argument("--fallback-caption", action="store_true",
                    help="use the manifest caption as /Alt when no generated alt text exists")
    ap.add_argument("--dry-run", action="store_true",
                    help="run the full matching and report coverage, but write nothing")
    args = ap.parse_args(argv)

    if not args.dry_run and args.output is None:
        ap.error("--output is required unless --dry-run is given")

    pdf_name = args.pdf_name or args.pdf.stem
    report = embed(args.pdf, args.manifest, args.alt_csv, args.output, pdf_name,
                   fallback_caption=args.fallback_caption, dry_run=args.dry_run)
    print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
