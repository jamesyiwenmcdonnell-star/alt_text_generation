import pikepdf
import csv
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = PROJECT_ROOT / "pdffigures2_out" / "manifest.csv"
PDF_PATH = PROJECT_ROOT / "PDFTesting" / "full_test.pdf"

seen_tags = set()
all_figures = []   # (page_key, parent_tag, alt_status) — every /Figure, regardless of parent


def as_list(x):
    """A /K-style value is either a single object or a pikepdf.Array. Normalize to a list."""
    if isinstance(x, pikepdf.Array):
        return list(x)
    return [x]


def resolve_role(tag, role_map):
    """Follow RoleMap chains to the standard tag name. Guards against cycles."""
    seen = set()
    while role_map is not None and tag in role_map and tag not in seen:
        seen.add(tag)
        tag = str(role_map[tag])
    return tag


def alt_status(kid) -> str:
    """Classify a structure element's accessibility text.
    'alt'        -> non-empty /Alt present (compliant)
    'actualtext' -> no /Alt, but non-empty /ActualText present (wrong field for a Figure)
    'empty'      -> /Alt present but blank/whitespace only
    'none'       -> neither field present
    """
    if not isinstance(kid, pikepdf.Dictionary):
        return "none"

    alt = kid.get("/Alt")
    if alt is not None:
        return "alt" if len(str(alt).strip()) > 0 else "empty"

    actual = kid.get("/ActualText")
    if actual is not None and len(str(actual).strip()) > 0:
        return "actualtext"

    return "none"


def describe_kid(kid, depth=0):
    """Full tree dump. Not called by default — use when a page's count looks wrong.
    /S checked before /Type: structure elements often carry /Type /StructElem
    too, and must not be mistaken for MCR/OBJR terminal nodes."""
    indent = "  " * depth

    if isinstance(kid, int):
        print(f"{indent}[MCID {kid}]")
        return

    if not isinstance(kid, pikepdf.Dictionary):
        print(f"{indent}[Unrecognized node: {kid}]")
        return

    if "/S" in kid:
        tag = kid.S
        alt = kid.get("/Alt")
        line = f"{indent}<{tag}>"
        if alt is not None:
            line += f"  Alt={alt!r}"
        print(line)

        if "/K" in kid:
            for child in as_list(kid.K):
                describe_kid(child, depth + 1)
        return

    if "/Type" in kid:
        kid_type = kid.Type
        if kid_type == "/MCR":
            print(f"{indent}[MCR page={kid.get('/Pg')} mcid={kid.get('/MCID')}]")
        elif kid_type == "/OBJR":
            print(f"{indent}[OBJR obj={kid.get('/Obj')}]")
        else:
            print(f"{indent}[Unrecognized /Type: {kid_type}]")
        return

    print(f"{indent}[Unrecognized node: {kid}]")


def resolve_page(element, depth=0):
    """Find the page this structure element actually sits on.
    /S checked before /Type: a Figure with /Type /StructElem must still get
    its /K recursed and its own /Pg checked, not be treated as a non-page-bearing
    MCR/OBJR node just because /Type happens to be present."""
    if depth > 20:
        return None

    if isinstance(element, int):
        return None

    if not isinstance(element, pikepdf.Dictionary):
        return None

    if "/S" in element:
        # Structure element: children first (more specific than an inherited /Pg),
        # then its own /Pg if present.
        if "/K" in element:
            for child in as_list(element.K):
                found = resolve_page(child, depth + 1)
                if found is not None:
                    return found
        if "/Pg" in element:
            return element.Pg
        return None

    if "/Type" in element:
        if element.Type == "/MCR" and "/Pg" in element:
            return element.Pg
        if element.Type == "/OBJR":
            obj = element.get("/Obj")
            if isinstance(obj, pikepdf.Dictionary) and "/P" in obj:
                return obj.P
        return None

    return None


def collect_caption_figures(kid, role_map, parent_tag=None, current_page=None, results=None):
    """/S checked before /Type -- see note above. This is what was silently
    dropping every figure on PDFs from tools that set /Type /StructElem."""
    if results is None:
        results = []

    if isinstance(kid, int):
        return results

    if not isinstance(kid, pikepdf.Dictionary):
        return results

    if "/S" in kid:
        tag = str(kid.S)
        seen_tags.add(tag)
        resolved_tag = resolve_role(tag, role_map)
        page = kid.get("/Pg", current_page)

        if resolved_tag.lower() == "/figure":
            page_obj = resolve_page(kid)
            page_key = page_obj.objgen if page_obj is not None else None
            status = alt_status(kid)
            all_figures.append((page_key, parent_tag, status))

            if parent_tag and resolve_role(parent_tag, role_map).lower() == "/caption":
                results.append((page_key, tag, status))

        if "/K" in kid:
            for child in as_list(kid.K):
                collect_caption_figures(child, role_map, parent_tag=tag, current_page=page, results=results)
        return results

    # Only reachable for non-structure-element dicts (MCR, OBJR)
    if "/Type" in kid:
        return results

    return results


def load_manifest_rows(pdf_stem):
    """Return manifest rows belonging to one PDF."""
    if not MANIFEST_PATH.exists():
        print(f"manifest not found at {MANIFEST_PATH}")
        return []

    with open(MANIFEST_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    print("manifest columns:", list(rows[0].keys()) if rows else "(empty)")

    for col in ("pdf", "pdf_name", "source_pdf", "pdf_path", "file"):
        if rows and col in rows[0]:
            return [r for r in rows if pdf_stem in r[col]]

    print("no pdf-identifying column found; using all rows")
    return rows


if __name__ == "__main__":
    pdf = pikepdf.open(PDF_PATH)

    page_lookup = {p.obj.objgen: i + 1 for i, p in enumerate(pdf.pages)}  # 1-indexed

    role_map = pdf.Root.StructTreeRoot.get("/RoleMap")

    results = []
    for kid in as_list(pdf.Root.StructTreeRoot.K):
        collect_caption_figures(kid, role_map, results=results)

    print("seen tags:", sorted(seen_tags))
    print("role map:", dict(role_map) if role_map is not None else "none declared")

    manifest_rows = load_manifest_rows(PDF_PATH.stem)

    print()
    print("caption-nested figures:", len(results))
    print("all /Figure tags (post-RoleMap resolution):", len(all_figures))
    print("parents of /Figure:", Counter(p for _, p, _ in all_figures))
    print("manifest rows:", len(manifest_rows))

    # per-page: structure tree vs manifest
    print()
    tree_counts = Counter(page_lookup.get(pk, "unknown") for pk, _, _ in all_figures)

    page_col = next(
        (c for c in ("page", "page_num", "pageNum") if manifest_rows and c in manifest_rows[0]),
        None,
    )
    if page_col is None:
        print("no page column found in manifest; columns:", list(manifest_rows[0].keys()) if manifest_rows else "(empty)")
        manifest_counts = Counter()
    else:
        manifest_counts = Counter(int(r[page_col]) + 1 for r in manifest_rows)

    for page in sorted(set(tree_counts) | set(manifest_counts), key=str):
        t, m = tree_counts.get(page, 0), manifest_counts.get(page, 0)
        flag = "" if t == m else "   <-- MISMATCH"
        print(f"page {page}: tree={t} manifest={m}{flag}")

    # vendor alt-text audit summary
    print()
    total = len(all_figures)
    status_counts = Counter(s for _, _, s in all_figures)
    n_alt = status_counts.get("alt", 0)
    n_actualtext = status_counts.get("actualtext", 0)
    n_empty = status_counts.get("empty", 0)
    n_none = status_counts.get("none", 0)

    if total == 0:
        overall = "NO FIGURES"
    elif n_alt == total:
        overall = "FULLY EMBEDDED"
    elif n_alt == 0 and n_actualtext == 0:
        overall = "NOT EMBEDDED"
    else:
        overall = "PARTIALLY / INCORRECTLY EMBEDDED"

    print(f"vendor alt-text audit: {overall}")
    print(f"  /Alt (compliant):          {n_alt}/{total}")
    print(f"  /ActualText only (wrong field, needs vendor fix): {n_actualtext}/{total}")
    print(f"  /Alt present but empty:    {n_empty}/{total}")
    print(f"  no accessibility text at all: {n_none}/{total}")