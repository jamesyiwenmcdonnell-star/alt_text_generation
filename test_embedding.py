import pikepdf

def describe_kid(kid, depth=0):
    indent = "  " * depth

    # Case 1: bare MCID (int) — leaf, no /S, nothing to recurse into
    if isinstance(kid, int):
        print(f"{indent}[MCID {kid}]")
        return

    # Case 2: dict with /Type -> /MCR or /OBJR — reference leaf, not a structure element
    if "/Type" in kid:
        kid_type = kid.Type
        if kid_type == "/MCR":
            print(f"{indent}[MCR page={kid.get('/Pg')} mcid={kid.get('/MCID')}]")
        elif kid_type == "/OBJR":
            print(f"{indent}[OBJR obj={kid.get('/Obj')}]")
        else:
            print(f"{indent}[Unrecognized /Type: {kid_type}]")
        return

    # Case 3: structure element — has /S, may have /Alt, may have its own /K
    if "/S" in kid:
        tag = kid.S
        alt = kid.get("/Alt")
        line = f"{indent}<{tag}>"
        if alt is not None:
            line += f"  Alt={alt!r}"
        print(line)

        if "/K" in kid:
            children = kid.K
            if not isinstance(children, pikepdf.Array):
                children = [children]
            for child in children:
                describe_kid(child, depth + 1)
        return

    print(f"{indent}[Unrecognized node: {kid}]")

def as_list(x):
    """A /K-style value is either a single object or a pikepdf.Array. Normalize to a list."""
    if isinstance(x, pikepdf.Array):
        return list(x)
    return [x]

seen_tags = set()

def resolve_page(element, depth=0):
    """Find the page this structure element actually sits on by descending to
    the first concrete page reference. Returns a pikepdf page object or None."""
    if depth > 20:
        return None

    if isinstance(element, int):
        return None

    if not isinstance(element, pikepdf.Dictionary):
        return None

    # MCR leaf — carries /Pg explicitly. Most reliable.
    if "/Type" in element:
        if element.Type == "/MCR" and "/Pg" in element:
            return element.Pg
        if element.Type == "/OBJR":
            obj = element.get("/Obj")
            # annotations carry /P pointing at their page
            if isinstance(obj, pikepdf.Dictionary) and "/P" in obj:
                return obj.P
        return None

    # Structure element: its own /Pg is only trustworthy if it has one AND
    # we can't find something more specific below. Check children first.
    if "/K" in element:
        for child in as_list(element.K):
            found = resolve_page(child, depth + 1)
            if found is not None:
                return found

    # Bare MCIDs under this element refer to content on this element's own /Pg
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

    if "/Type" in kid:
        return results

    if "/S" in kid:
        tag = str(kid.S)
        seen_tags.add(tag)
        page = kid.get("/Pg", current_page)

        if tag.lower() == "/figure" and parent_tag and parent_tag.lower() == "/caption":
            page_obj = resolve_page(kid)
            page_key = page_obj.objgen if page_obj is not None else None
            results.append((page_key, tag))

        if "/K" in kid:
            for child in as_list(kid.K):
                collect_caption_figures(child, parent_tag=tag, current_page=page, results=results)
        return results

    return results



if __name__ == "__main__":
    pdf = pikepdf.open("PDFTesting/full_test.pdf")

    page_lookup = {p.obj.objgen: i + 1 for i, p in enumerate(pdf.pages)}  # 1-indexed

    results = []
    for kid in as_list(pdf.Root.StructTreeRoot.K):
        collect_caption_figures(kid, results=results)

    print("seen tags:", sorted(seen_tags))

    role_map = pdf.Root.StructTreeRoot.get("/RoleMap")
    print("role map:", dict(role_map) if role_map is not None else "none declared")

    from collections import Counter
    counts = Counter(page_key for page_key, _ in results)
    for page_key, n in sorted(counts.items(), key=lambda kv: page_lookup.get(kv[0], -1)):
        page_num = page_lookup.get(page_key, "unknown")
        print(f"page {page_num}: {n} caption→figure pairs")