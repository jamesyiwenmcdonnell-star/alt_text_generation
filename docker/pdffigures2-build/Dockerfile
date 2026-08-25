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
# RunPod REST + GraphQL calls), runpod_VL.py (needs openai to talk to the
# vLLM pod's OpenAI-compatible endpoint), and api_server.py (needs fastapi +
# uvicorn for the intake API, python-multipart for its file-upload endpoint --
# plain uvicorn, not uvicorn[standard]: its extras aren't needed for a
# single-worker, no-hot-reload internal API). job_store.py/worker.py use only
# sqlite3 (stdlib) and telegram_status.py talks to Telegram's Bot API via
# plain requests -- neither needs a new package. PyPI ships prebuilt manylinux
# wheels for pikepdf on both amd64 and arm64, so this shouldn't need a
# compiler -- if it ever falls back to building from source, add `qpdf` +
# `build-essential` to the apt install above.
RUN pip3 install --no-cache-dir pikepdf requests openai fastapi uvicorn python-multipart

# JAI ImageIO's JPEG2000 plugin -- vanilla PDFBox/pdffigures2 can't decode
# JPEG2000-encoded images without it ("Cannot read JPEG2000 image: Java
# Advanced Imaging (JAI) Image I/O Tools are not installed"), which silently
# drops every figure embedded that way -- can undercount figures by an order
# of magnitude on scan-heavy PDFs. Oracle's original JAI ImageIO Tools were
# never on Maven Central (native/licensing reasons); this is the pure-Java
# community fork that is: https://github.com/jai-imageio/jai-imageio-jpeg2000
# `-jar` ignores -cp/CLASSPATH entirely, so using these requires invoking
# pdf_batch_runner.py with --extra-classpath "$JAI_JPEG2000_CLASSPATH" to
# switch the java invocation from -jar to -cp + main class.
RUN mkdir -p /opt/jai && \
    curl -fsSL -o /opt/jai/jai-imageio-core-1.4.0.jar \
      https://repo1.maven.org/maven2/com/github/jai-imageio/jai-imageio-core/1.4.0/jai-imageio-core-1.4.0.jar && \
    curl -fsSL -o /opt/jai/jai-imageio-jpeg2000-1.4.0.jar \
      https://repo1.maven.org/maven2/com/github/jai-imageio/jai-imageio-jpeg2000/1.4.0/jai-imageio-jpeg2000-1.4.0.jar
ENV JAI_JPEG2000_CLASSPATH=/opt/jai/jai-imageio-core-1.4.0.jar:/opt/jai/jai-imageio-jpeg2000-1.4.0.jar

WORKDIR /build

# Default action: build the assembly jar. Override with e.g. `docker run ... pdffigures2-builder sbt console`
# or with an --entrypoint override for an interactive shell (see shell.sh).
ENTRYPOINT ["sbt"]
CMD ["assembly"]
