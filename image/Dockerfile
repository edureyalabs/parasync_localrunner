# image/Dockerfile
# The Linux environment that runs agent tool code on a person's PC. It is built here, exported as a root file system, and imported by
# the desktop app as its own WSL2 distribution ("ParasyncRunner"). Build from the repository root:
#   docker build -f image/Dockerfile -t parasync-runner .
#
# Python 3.9 on purpose: the cloud sandbox runs 3.9 (Amazon Linux 2023), and a script must behave the same in both places. Move
# both to a newer version together, never one.
FROM python:3.9-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LANG=C.UTF-8

# Small and boring: a way to kill a runaway job (coreutils' `timeout`, already present), certificates for HTTPS, time zones, and the fonts
# reportlab-made PDFs expect. No compilers, no sudo, no package managers a script could abuse beyond pip (which scripts may use, as in the cloud).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tzdata fonts-liberation \
 && rm -rf /var/lib/apt/lists/*

COPY image/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt && rm /tmp/requirements.txt

# Browser sessions (image 0.2.0): Chromium for Playwright, with the system libraries it needs, installed once here and readable by the
# unprivileged user. WSL does not carry Docker's ENV into the exported system, so browser_host.py sets PLAYWRIGHT_BROWSERS_PATH itself.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN python3 -m playwright install --with-deps chromium \
 && chmod -R a+rX /opt/pw-browsers \
 && rm -rf /var/lib/apt/lists/*

# The one program the desktop app starts for each job, and the harness it uses (a copy of the cloud's, see scripts/sync_harness.py).
COPY runner/ /opt/runner/
COPY VERSION /etc/parasync-runner-version
COPY image/wsl.conf /etc/wsl.conf

# The unprivileged user scripts run as; its home is a throw-away folder.
RUN useradd --create-home --shell /bin/bash --uid 1000 runner \
 && chown -R root:root /opt/runner && chmod -R a-w /opt/runner \
 && mkdir -p /tmp/parasync && chmod 1777 /tmp /tmp/parasync

WORKDIR /opt/runner
USER runner
CMD ["python3", "/opt/runner/run_job.py"]
