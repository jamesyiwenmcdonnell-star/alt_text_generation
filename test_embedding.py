import pikepdf
import csv
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = PROJECT_ROOT / "pdffigures2_out" / "manifest.csv"
PDF_PATH = PROJECT_ROOT / "PDFTesting" / "full_test.pdf"

seen_tags = set()
all_figures = []   # (page_key, parent_tag) — every /Figure, regardless of parent


def describe_kid(kid, depth=0):
    """Full tree dump. Not called by default — use when a page's count looks wrong."""
    indent = "  " * depth

    if isinstance(kid, int):
        print(f"{indent}[MCID {kid}]")
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

    print(f"{indent}[Unrecognized node: {kid}]")


def as_list(x):
    """A /K-style value is either a single object or a pikepdf.Array. Normalize to a list."""
    if isinstance(x, pikepdf.Array):
        return list(x)
    return [x]


def resolve_page(element, depth=0):
    if depth > 20:
        return None
    if isinstance(element, int):
        return None
    if not isinstance(element, pikepdf.Dictionary):
        return None

    # Only MCR/OBJR are true leaves; /Type /StructElem is still a structure element
    if "/S" not in element:
        if element.get("/Type") == "/MCR" and "/Pg" in element:
            return element.Pg
        if element.get("/Type") == "/OBJR":
            obj = element.get("/Obj")
            if isinstance(obj, pikepdf.Dictionary) and "/P" in obj:
                return obj.P
        return None

    if "/K" in element:
        for child in as_list(element.K):
            found = resolve_page(child, depth + 1)
            if found is not None:
                return found

    if "/Pg" in element:
        return element.Pg

    return None


def collect_caption_figures(kid, parent_tag=None, current_page=None, results=None):
    if results is None:
        results = []

    if isinstance(kid, int):
        return results

    if not isinstance(kid, pikepdf.Dictionary):
        return results

    # MCR / OBJR are the only real leaf types — everything else with /Type
    # may still be a structure element carrying /Type /StructElem
    if "/S" not in kid:
        if kid.get("/Type") in ("/MCR", "/OBJR"):
            return results
        print(f"  [dict with no /S, /Type={kid.get('/Type')}, keys={list(kid.keys())}]")
        return results

    tag = str(kid.S)
    seen_tags.add(tag)
    page = kid.get("/Pg", current_page)

    if tag.lower() == "/figure":
        page_obj = resolve_page(kid)
        page_key = page_obj.objgen if page_obj is not None else None
        all_figures.append((page_key, parent_tag))

    if "/K" in kid:
        for child in as_list(kid.K):
            collect_caption_figures(child, parent_tag=tag, current_page=page, results=results)

    return results


def load_manifest_rows(pdf_stem):
    """Return manifest rows belonging to one PDF."""
    if not MANIFEST_PATH.exists():
        print(f"manifest not found at {MANIFEST_PATH}")
        return []

    with open(MANIFEST_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    print("manifest columns:", list(rows[0].keys()) if rows else "(empty)")

    # filter to this PDF — column name varies, so match on whichever holds the source name
    for col in ("pdf", "pdf_name", "source_pdf", "pdf_path", "file"):
        if rows and col in rows[0]:
            return [r for r in rows if pdf_stem in r[col]]

    print("no pdf-identifying column found; using all rows")
    return rows


if __name__ == "__main__":
    pdf = pikepdf.open(PDF_PATH)

    if "/StructTreeRoot" not in pdf.Root:
        print(f"{PDF_PATH.name}: NOT TAGGED — untagged backlog, not matchable")
        raise SystemExit

    # --- Check 2: do image XObjects exist on the pages at all? ---
    print("--- image XObjects per page ---")
    for i, page in enumerate(pdf.pages, start=1):
        xobjs = page.get("/Resources", {}).get("/XObject", {})
        images = [k for k, v in xobjs.items()
                  if isinstance(v, pikepdf.Stream) and v.get("/Subtype") == "/Image"]
        annots = len(page.get("/Annots", []))
        if images:
            print(f"page {i}: {len(images)} image XObject(s), {annots} annots")

    # --- Check 3: are those images linked back into the structure tree? ---
    print()
    print("--- /StructParent on image XObjects ---")
    parent_tree = pdf.Root.StructTreeRoot.get("/ParentTree")
    print("has ParentTree:", parent_tree is not None)

    for i, page in enumerate(pdf.pages, start=1):
        xobjs = page.get("/Resources", {}).get("/XObject", {})
        for name, xobj in xobjs.items():
            if isinstance(xobj, pikepdf.Stream) and xobj.get("/Subtype") == "/Image":
                print(f"page {i} {name}: /StructParent =", xobj.get("/StructParent", "absent"))


    #<-------old code------>

    page_lookup = {p.obj.objgen: i + 1 for i, p in enumerate(pdf.pages)}  # 1-indexed

    results = []
    for kid in as_list(pdf.Root.StructTreeRoot.K):
        collect_caption_figures(kid, results=results)

    print("seen tags:", sorted(seen_tags))

    role_map = pdf.Root.StructTreeRoot.get("/RoleMap")
    print("role map:", dict(role_map) if role_map is not None else "none declared")

    manifest_rows = load_manifest_rows(PDF_PATH.stem)

    print()
    print("caption-nested figures:", len(results))
    print("all /Figure tags:", len(all_figures))
    print("parents of /Figure:", Counter(p for _, p in all_figures))
    print("manifest rows:", len(manifest_rows))

    # per-page: structure tree vs manifest
    print()
    tree_counts = Counter(page_lookup.get(pk, "unknown") for pk, _ in all_figures)

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
        if t == 0 and m == 0:
            continue
        flag = "" if t == m else "   <-- MISMATCH"
        print(f"page {page}: tree={t} manifest={m}{flag}")

