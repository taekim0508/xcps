#!/usr/bin/env bash
# Run a command inside the traffic-cps Linux container with the repo mounted.
#   ./dev.sh build                         build (or rebuild) the image
#   ./dev.sh traffic-cps doctor            any command, run in the container
#   ./dev.sh python -m pytest tests/unit
set -euo pipefail

IMAGE=traffic-cps-dev:latest
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${1:-}" == "build" ]]; then
    exec docker build -t "$IMAGE" "$ROOT"
fi

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "Image $IMAGE not found. Run: ./dev.sh build" >&2
    exit 2
fi

TTY_FLAGS=(-i)
[[ -t 0 && -t 1 ]] && TTY_FLAGS=(-it)

# The ${arr[@]+...} form keeps macOS bash 3.2 happy with an empty array under set -u.
exec docker run --rm ${TTY_FLAGS[@]+"${TTY_FLAGS[@]}"} \
    -v "$ROOT":/work -w /work \
    --user "$(id -u):$(id -g)" -e HOME=/tmp \
    "$IMAGE" "$@"
