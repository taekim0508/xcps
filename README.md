# traffic-cps

Explainable traffic signal controller for one simulated intersection (SUMO + direct TraCI). Work in progress; see `docs/build-progress.md` for status and `docs/traffic-explainability-design.md` for the specification.

## Setup

SUMO 1.24.0 runs in a Linux container (the macOS wheel needs Homebrew library versions that are no longer available). Requires Docker Desktop running.

```sh
./dev.sh build                          # build the image (rerun after dependency changes)
./dev.sh traffic-cps doctor             # verify the pinned toolchain
./dev.sh traffic-cps scenario build --output scenarios/single
./dev.sh python -m pytest tests/unit
./dev.sh python -m pytest tests/integration
```

## Limits

Simulated, one-second, discrete time. No all-red clearance. Sensing reads simulator ground truth by default. Not suitable for road deployment.
