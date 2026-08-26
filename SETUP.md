# Project setup — pdffigures2 alt-text extraction pipeline

Everything runs inside one Docker container (JDK 17 + Python 3 + pikepdf), so the only things
installed directly on your Mac are Homebrew, Docker (via Colima), and git.

## 1. Install

```bash
brew install docker colima git
colima start --cpu 4 --memory 8
```

`colima start` needs to be re-run after every reboot (it doesn't auto-start like Docker Desktop
would). Check it's up with `docker info` before continuing — it should return engine info with
no connection errors.

## 2. File structure

Create a project folder (this doc uses `PROJECT/` — yours might be `internVL/` or similar) laid
out like this:

```
PROJECT/
├── docker/pdffigures2-build/
│   ├── Dockerfile          # JDK 17 + sbt + python3 + pikepdf image
│   ├── build.sh            # builds pdffigures2.jar via the container
│   └── shell.sh            # interactive shell inside the container
├── pdffigures2/             # git clone of allenai/pdffigures2 (built jar lands here)
├── PDFTesting/               # your source PDFs
├── pdf_batch_runner.py      # extraction orchestrator (finds PDFs, runs pdffigures2, builds manifest + validation report)
├── diagnose_tagging.py      # checks whether PDFs are already tagged, before attempting alt-text re-embedding
└── pdffigures2_out/          # generated on first run: figures/, data/, stats/, logs/, manifest.csv, validation_report.csv
```

`docker/pdffigures2-build/*` and the two `.py` files are the ones delivered during this project's
setup — place them at the paths above. `pdffigures2_out/` and `pdffigures2/pdffigures2.jar` are
generated, not something to create by hand.

## 3. One-time build

```bash
cd PROJECT

# get the source
git clone https://github.com/allenai/pdffigures2.git pdffigures2

# known build fix: the sbt-bintray plugin references a dead service (Bintray
# shut down in 2021); strip it and the settings that depend on it -- neither
# is used by the `assembly` task this project actually needs
sed -i '' '/sbt-bintray/d' pdffigures2/project/plugins.sbt
sed -i '' '/bintray/d' pdffigures2/build.sbt

# build the container image (optional -- startup.py, build.sh and shell.sh all
# run this same build themselves, including after a Dockerfile change; Docker's
# layer cache makes the unchanged case a fast no-op)
docker build -t pdffigures2-builder ./docker/pdffigures2-build

# build pdffigures2.jar inside the container
chmod +x docker/pdffigures2-build/build.sh docker/pdffigures2-build/shell.sh
./docker/pdffigures2-build/build.sh
```

Confirm it worked: `ls pdffigures2/pdffigures2.jar` should exist.

## 4. First start

```bash
./docker/pdffigures2-build/shell.sh
```

This drops you into a bash shell inside the container with the whole `PROJECT/` folder mounted
at `/work`. From there, run the pipeline:

```bash
python3 pdf_batch_runner.py \
  --input-dir ./PDFTesting \
  --jar ./pdffigures2/pdffigures2.jar \
  --output-dir ./pdffigures2_out \
  --dpi 300 --java-heap 6g -v

python3 diagnose_tagging.py \
  --input-dir ./PDFTesting \
  --pdffigures2-data ./pdffigures2_out/data \
  --output-csv ./tagging_report.csv --dump-elements
```

`exit` when done — the container is disposable (`--rm`); nothing is lost since all real files
live in the mounted `PROJECT/` folder, not inside the container itself.

## 5. What to check afterward

- `pdffigures2_out/manifest.csv` — every detected figure/table across all PDFs, with image path,
  page, caption, bounding box. Feeds the Phase 2 model-eval harness.
- `pdffigures2_out/validation_report.csv` — flags gaps in figure numbering (e.g. `1.1, 1.3` with
  no `1.2`), a signal pdffigures2 likely missed a real figure.
- `tagging_report.csv` — per PDF, whether it's already tagged and how its structure-tree figure
  count compares to what pdffigures2 found. Determines whether alt-text re-embedding (the next
  pipeline stage, not yet built) is a tractable in-house task for that PDF or not.

## Re-running later

Adding more PDFs to `PDFTesting/` doesn't require rebuilding anything — just re-enter the shell
(`./docker/pdffigures2-build/shell.sh`) and re-run the same `pdf_batch_runner.py` command with
`--skip-done` added, to only process the new ones:

```bash
python3 pdf_batch_runner.py --input-dir ./PDFTesting --jar ./pdffigures2/pdffigures2.jar --output-dir ./pdffigures2_out --skip-done
```
