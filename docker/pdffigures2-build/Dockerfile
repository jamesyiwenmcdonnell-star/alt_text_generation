# Build environment for pdffigures2 -- pins JDK 17 so `sbt assembly` never hits
# https://github.com/scala/bug/issues/12783 (scalac's classfile parser crashing on
# JDK 21+ system classes). Nothing here is pdffigures2-specific except the base
# JDK choice; the actual sbt engine version comes from the project's own
# project/build.properties (sbt.version=1.7.1), fetched automatically on first run.
#
# Build once:   docker build -t pdffigures2-builder ./docker/pdffigures2-build
# Use it:       see build.sh in this same folder.

FROM eclipse-temurin:17-jammy

# Official sbt apt-repo setup, from https://www.scala-sbt.org/1.x/docs/Installing-sbt-on-Linux.html
RUN apt-get update && \
    apt-get install -y --no-install-recommends apt-transport-https curl gnupg ca-certificates git \
      python3 python3-pip && \
    echo "deb https://repo.scala-sbt.org/scalasbt/debian all main" > /etc/apt/sources.list.d/sbt.list && \
    echo "deb https://repo.scala-sbt.org/scalasbt/debian /" > /etc/apt/sources.list.d/sbt_old.list && \
    curl -sL "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x2EE0EA64E40A89B84B2DF73499E82A75642AC823" \
      | gpg --no-default-keyring --keyring gnupg-ring:/etc/apt/trusted.gpg.d/scalasbt-release.gpg --import && \
    chmod 644 /etc/apt/trusted.gpg.d/scalasbt-release.gpg && \
    apt-get update && \
    apt-get install -y --no-install-recommends sbt && \
    rm -rf /var/lib/apt/lists/*

# For pdf_batch_runner.py (stdlib only, nothing to install), diagnose_tagging.py
# (needs pikepdf), controller.py/pod.py/gpu_utils.py (need requests for the
# RunPod REST + GraphQL calls), and runpod_VL.py (needs openai to talk to the
# vLLM pod's OpenAI-compatible endpoint). PyPI ships prebuilt manylinux wheels
# for pikepdf on both amd64 and arm64, so this shouldn't need a compiler -- if
# it ever falls back to building from source, add `qpdf` + `build-essential`
# to the apt install above.
RUN pip3 install --no-cache-dir pikepdf requests openai

WORKDIR /build

# Default action: build the assembly jar. Override with e.g. `docker run ... pdffigures2-builder sbt console`
# or with an --entrypoint override for an interactive shell (see shell.sh).
ENTRYPOINT ["sbt"]
CMD ["assembly"]
