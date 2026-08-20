"""

MODELS CONFIGURED
------------------
InternVL3.5-8B

USAGE
-----
1. For each model in MODEL_CONFIGS, set "pod_id" to the id in that pod's
   RunPod proxy URL (the part before "-8000.proxy.runpod.net", e.g.
   "gn4axx2p5q2qyo"). That's the only thing you need to change when you
   spin a model up on a new pod.
2. Optionally fill in a per-model "system_prompt". Leave it as "" to fall
   back to DEFAULT_SYSTEM_PROMPT.
3. Edit IMAGE_DIR to point at the folder containing your test images
   (png/jpg/jpeg/webp).
4. Set TEST_MODE = True to smoke-test a single endpoint with one image
   (prints the raw description, no CSV). TEST_MODE always uses
   TEST_MODE_MODEL (InternVL3.5-8B by default), regardless of what you'd
   otherwise pick, so you don't need every pod up just to sanity-check one.
5. Run:  python vl_comparison.py
   When TEST_MODE is off, the script asks whether to run every model
   sequentially or just one — pick by number or by name.

Output: results/vl_comparison_<model>_<timestamp>.csv, one file per model.
"""

import base64
import csv
import glob
import os
import time
from datetime import datetime

from openai import OpenAI


# ---------------------------------------------------------------------------
# CONFIG — edit these
# ---------------------------------------------------------------------------

# One entry per model. To switch which pod a model points at, just change
# its "pod_id" — that's the identifier in the pod's RunPod proxy URL
# (https://<pod_id>-<port>.proxy.runpod.net/v1).
#
# "system_prompt" left as "" falls back to DEFAULT_SYSTEM_PROMPT below.
# Fill one in per-model once you want to try model-specific prompting.

MODEL_CONFIGS = {
    "InternVL3.5-8B": {
        "pod_id": "gn4axx2p5q2qyo",
        "port": 8000,
        "api_key": "EMPTY",
        "system_prompt": (
            "You are an accessibility image-description assistant. You generate alt text that "
            "lets a non-sighted reader reconstruct the layout and content of an image in their "
            "own mind — not just its meaning or purpose, but its actual visual structure: what "
            "is where, what it says, what color it is, and how the pieces connect.  CORE METHOD: "
            "DESCRIBE BY READING ORDER AND POSITION 1. Open by stating the overall layout and "
            "reading order — e.g., \"left to right,\" \"top to bottom,\" how many distinct "
            "regions/objects the image contains and where each sits relative to the others. 2. "
            "Then walk through each region in that order. For a structured diagram (pyramid, "
            "funnel, flowchart, stacked chart, infographic), move systematically through its "
            "layers or sections (e.g., base to apex, or start to end) rather than jumping "
            "around. 3. For each element, state in this order where relevant: its position, its "
            "color, any text it contains (read closely, near-verbatim, both primary label and "
            "any smaller secondary text), and any icons, symbols, or motifs attached to it and "
            "where they sit (e.g., \"near the right edge,\" \"at the base\"). 4. Describe "
            "connecting elements explicitly: arrows (direction, curvature, what they run "
            "from/to, their label if any), dashed lines, paths, arcs — state their position and "
            "trajectory, not just that they exist. 5. If the image contains a secondary scene "
            "or illustration alongside a diagram, describe it after the diagram, in the same "
            "manner: main subject, their action/pose, objects around them, whats on any screen "
            "or surface, then background elements.  WHAT TO INCLUDE - Exact or near-exact text "
            "as it appears, including small/secondary text under headings - Named colors for "
            "distinct bands, sections, or elements (not just \"colored\" — say dark blue, "
            "orange, pale blue, etc.) - Icons and symbols, described literally (a computer "
            "monitor icon, a magnifying glass over a plot, a wind/flow symbol) with their "
            "position - Spatial relationships between every element and its neighbors, so the "
            "structure could be redrawn from the text alone  WHAT TO AVOID - Do not open with "
            "\"Image of,\" \"This shows,\" \"A diagram depicting,\" or any framing phrase — go "
            "straight into the description - Do not interpret meaning, intent, or takeaway "
            "(\"this represents growth,\" \"this suggests progress\") — describe only what is "
            "visually depicted - Do not compress a data-dense or multi-section image down to a "
            "vague one-line gloss — within the word limit below, prioritize the overall layout, "
            "key text, and structural relationships over exhaustive minor detail - Do not use "
            "bullet points, headers, or line breaks — output one continuous descriptive "
            "paragraph - Do not add commentary on style or aesthetics unless explicitly asked  "
            "OUTPUT Return only the description itself, as a single flowing paragraph, no "
            "preamble and no closing remarks. The description must be 120 to 150 words — no "
            "shorter and no longer, regardless of image complexity. For a simple image, use the "
            "full range to add spatial and structural detail; for a complex multi-part image, "
            "select and prioritize only the most important regions, text, and connections to "
            "fit within this limit."
        ),
    },
}

# Model TEST_MODE always uses, regardless of the interactive prompt.
TEST_MODE_MODEL = "InternVL3.5-8B"

# Folder containing the images to test (png/jpg/jpeg/webp). Create this
# folder and add your test images, or point this at wherever they live.
IMAGE_DIR = os.path.expanduser("~/Desktop/testImages")

# Where to write the results CSVs.
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")

# Set True to smoke-test your setup: sends ONE image to TEST_MODE_MODEL,
# prints the raw description + timing, and exits — no CSV, no looping over
# the full image set, no interactive prompt. Use this first to confirm
# connectivity and the chat template are working before burning time on a
# full run.
TEST_MODE = False

# Image used in TEST_MODE. If None, falls back to the first image found in
# IMAGE_DIR (or the file below, if IMAGE_DIR is empty).
TEST_IMAGE = os.path.expanduser("~/Desktop/test.png")

# Used for any model whose "system_prompt" above is left as "".
DEFAULT_SYSTEM_PROMPT = (
    "Do not think, reason, or plan before answering. Never output a <think> block, "
    "chain-of-thought, reasoning trace, draft, or any meta-commentary about how you "
    "produced the description. Respond immediately with only the final description "
    "text — nothing else. "
    "You are an accessibility image-description assistant. You generate alt text that "
    "lets a non-sighted reader reconstruct the layout and content of an image in their "
    "own mind — not just its meaning or purpose, but its actual visual structure: what "
    "is where, what it says, what color it is, and how the pieces connect.  CORE METHOD: "
    "DESCRIBE BY READING ORDER AND POSITION 1. Open by stating the overall layout and "
    "reading order — e.g., \"left to right,\" \"top to bottom,\" how many distinct "
    "regions/objects the image contains and where each sits relative to the others. 2. "
    "Then walk through each region in that order. For a structured diagram (pyramid, "
    "funnel, flowchart, stacked chart, infographic), move systematically through its "
    "layers or sections (e.g., base to apex, or start to end) rather than jumping "
    "around. 3. For each element, state in this order where relevant: its position, its "
    "color, any text it contains (read closely, near-verbatim, both primary label and "
    "any smaller secondary text), and any icons, symbols, or motifs attached to it and "
    "where they sit (e.g., \"near the right edge,\" \"at the base\"). 4. Describe "
    "connecting elements explicitly: arrows (direction, curvature, what they run "
    "from/to, their label if any), dashed lines, paths, arcs — state their position and "
    "trajectory, not just that they exist. 5. If the image contains a secondary scene "
    "or illustration alongside a diagram, describe it after the diagram, in the same "
    "manner: main subject, their action/pose, objects around them, whats on any screen "
    "or surface, then background elements.  WHAT TO INCLUDE - Exact or near-exact text "
    "as it appears, including small/secondary text under headings - Named colors for "
    "distinct bands, sections, or elements (not just \"colored\" — say dark blue, "
    "orange, pale blue, etc.) - Icons and symbols, described literally (a computer "
    "monitor icon, a magnifying glass over a plot, a wind/flow symbol) with their "
    "position - Spatial relationships between every element and its neighbors, so the "
    "structure could be redrawn from the text alone  WHAT TO AVOID - Do not open with "
    "\"Image of,\" \"This shows,\" \"A diagram depicting,\" or any framing phrase — go "
    "straight into the description - Do not interpret meaning, intent, or takeaway "
    "(\"this represents growth,\" \"this suggests progress\") — describe only what is "
    "visually depicted - Do not summarize or compress a data-dense or multi-section "
    "image down to a short gloss — completeness of layout and text takes priority over "
    "brevity here - Do not use bullet points, headers, or line breaks — output one "
    "continuous descriptive paragraph - Do not add commentary on style or aesthetics "
    "unless explicitly asked - Do not include any <think> tags, reasoning steps, "
    "planning notes, or explanations of your own process anywhere in the output  OUTPUT "
    "Return only the description itself, as a single flowing paragraph, no preamble, no "
    "reasoning, no <think> tags, and no closing remarks. Length should scale with the "
    "images complexity — a simple photo may only need two or three sentences, but a "
    "multi-part diagram or infographic should be described fully and systematically, "
    "section by section, even if that runs long."
)

USER_PROMPT = "Describe the image."
TEMPERATURE = 0.0
MAX_TOKENS = 1024
REQUEST_TIMEOUT = 120  # seconds
MAX_RETRIES = 1

IMAGE_EXTENSIONS = ("*.png", "*.jpg", "*.jpeg", "*.webp")

# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


def find_images(image_dir):
    paths = []
    for pattern in IMAGE_EXTENSIONS:
        paths.extend(glob.glob(os.path.join(image_dir, pattern)))
    return sorted(paths)


def encode_image(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def mime_type(path):
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    return "jpeg" if ext == "jpg" else ext


def resolve_model_cfg(name):
    """Turn a MODEL_CONFIGS entry into everything needed to actually hit the
    endpoint: a full base_url built from pod_id/port, and an effective
    system prompt (falls back to DEFAULT_SYSTEM_PROMPT if left blank)."""
    raw = MODEL_CONFIGS[name]
    base_url = f"https://{raw['pod_id']}-{raw.get('port', 8000)}.proxy.runpod.net/v1"
    system_prompt = raw.get("system_prompt") or DEFAULT_SYSTEM_PROMPT
    return {
        "name": name,
        "base_url": base_url,
        "api_key": raw.get("api_key", "EMPTY"),
        "system_prompt": system_prompt,
    }


def connect_model(cfg):
    """Open a client for this endpoint and resolve the model id it's
    actually serving (mirrors client.models.list().data[0].id)."""
    client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"])
    try:
        model_id = client.models.list().data[0].id
        print(f"[{cfg['name']}] connected -> serving model id: {model_id}")
        return client, model_id
    except Exception as exc:  # noqa: BLE001
        print(f"[{cfg['name']}] WARNING: could not reach {cfg['base_url']} ({exc}).")
        return client, None


def describe_image(client, model_id, image_path, system_prompt):
    b64_image = encode_image(image_path)
    mime = mime_type(image_path)

    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        start = time.perf_counter()
        try:
            response = client.chat.completions.create(
                model=model_id,
                messages=[
                    {"role": "system", "content": [{"type": "text", "text": system_prompt}]},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": USER_PROMPT},
                            {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{b64_image}"}},
                        ],
                    },
                ],
                temperature=TEMPERATURE,
                max_tokens=MAX_TOKENS,
                timeout=REQUEST_TIMEOUT,
            )
            elapsed = time.perf_counter() - start
            return response.choices[0].message.content.strip(), elapsed, None
        except Exception as exc:  # noqa: BLE001 — want to record any failure, keep going
            last_error = exc
            elapsed = time.perf_counter() - start
            print(f"    attempt {attempt + 1} failed after {elapsed:.1f}s: {exc}")

    return None, None, str(last_error)


def run_test_mode():
    """Smoke test: one image, TEST_MODE_MODEL only, full output printed so
    you can eyeball that the server responds correctly."""
    test_image = TEST_IMAGE
    if not test_image or not os.path.isfile(test_image):
        candidates = find_images(IMAGE_DIR)
        if not candidates:
            print(f"No test image found. Set TEST_IMAGE to a valid path, or add an image to {IMAGE_DIR}.")
            return
        test_image = candidates[0]

    cfg = resolve_model_cfg(TEST_MODE_MODEL)
    print(f"TEST_MODE on — using {test_image}")
    print(f"\n=== {cfg['name']} ({cfg['base_url']}) ===")

    client, model_id = connect_model(cfg)
    if model_id is None:
        print("  SKIPPED — endpoint unreachable (see warning above)")
        return

    description, elapsed, error = describe_image(client, model_id, test_image, cfg["system_prompt"])
    if error:
        print(f"  FAILED after retries: {error}")
    else:
        print(f"  OK — {elapsed:.1f}s")
        print(f"  ---\n  {description}\n  ---")


def run_model(cfg, images):
    """Run every image through one model. Returns a list of row dicts, or
    None if the endpoint was unreachable."""
    client, model_id = connect_model(cfg)
    if model_id is None:
        print(f"Skipping {cfg['name']} — endpoint unreachable.")
        return None

    rows = []
    for image_path in images:
        image_name = os.path.basename(image_path)
        print(f"  -> {image_name} ...", end=" ", flush=True)
        description, elapsed, error = describe_image(client, model_id, image_path, cfg["system_prompt"])
        if error:
            print(f"FAILED ({error})")
            rows.append({"image": image_name, "description": f"ERROR: {error}", "time_sec": ""})
        else:
            print(f"done in {elapsed:.1f}s")
            rows.append({"image": image_name, "description": description, "time_sec": round(elapsed, 2)})
    return rows


def write_csv(model_name, rows):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = model_name.replace(" ", "_").replace("/", "-")
    out_path = os.path.join(OUTPUT_DIR, f"vl_comparison_{safe_name}_{timestamp}.csv")

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["image", "description", "time_sec"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"Saved results to {out_path}")
    return out_path


def prompt_model_choice():
    """Ask whether to run every model sequentially or just one. Returns a
    list of model names to run, in order."""
    names = list(MODEL_CONFIGS.keys())
    all_index = len(names) + 1

    print("\nAvailable models:")
    for i, name in enumerate(names, 1):
        print(f"  {i}. {name}")
    print(f"  {all_index}. ALL (run sequentially)")

    choice = input(f"\nRun all models sequentially, or pick one [1-{all_index}, or type a name]: ").strip()

    if choice.lower() in ("all", "a", str(all_index)):
        return names
    if choice.isdigit() and 1 <= int(choice) <= len(names):
        return [names[int(choice) - 1]]
    if choice in MODEL_CONFIGS:
        return [choice]

    print(f"Unrecognized choice '{choice}' — defaulting to ALL.")
    return names


def main():
    if TEST_MODE:
        run_test_mode()
        return

    images = find_images(IMAGE_DIR)
    if not images:
        print(f"No images found in {IMAGE_DIR} (looked for {', '.join(IMAGE_EXTENSIONS)}).")
        print("Create the folder and add your test images, or edit IMAGE_DIR at the top of this script.")
        return

    print(f"Found {len(images)} image(s) in {IMAGE_DIR}")

    selected = prompt_model_choice()
    print(f"\nRunning: {', '.join(selected)}")

    for name in selected:
        cfg = resolve_model_cfg(name)
        print(f"\n=== {name} ({cfg['base_url']}) ===")
        rows = run_model(cfg, images)
        if rows:
            write_csv(name, rows)


if __name__ == "__main__":
    main()