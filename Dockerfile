# Linux runtime for traffic-cps. The macOS SUMO 1.24.0 wheel links against
# Homebrew library versions that Homebrew no longer ships, so SUMO runs here.
# Source is bind-mounted at /work by dev.sh; the editable install below points
# at that mount, so code edits on the host need no rebuild. Rebuild after
# changing pyproject.toml dependencies.
FROM python:3.11.17-slim

# Shared libraries the manylinux SUMO binaries load even when run headless.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libx11-6 libxext6 libxrender1 libgl1 libexpat1 \
    && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1
RUN python -m venv /opt/venv

WORKDIR /work
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e '.[dev]'
