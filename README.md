<!-- README.md -->
# Parasync local runner

The Linux environment that runs agent tool code on a person's own Windows PC, instead of in the cloud.

## What it is
- `runner/runner.py`: a verbatim copy of the cloud sandbox's harness (never edit by hand; `scripts/sync_harness.py` refreshes it and records the source commit in `runner/SYNCED_FROM.json`).
- `runner/run_job.py`: the one program the desktop app starts per job. It reads one JSON job on stdin, runs it through the harness, and prints exactly one `__PARASYNC_JOB_RESULT__{json}` line on stdout.
- `image/`: the Dockerfile (Python 3.9 to match the cloud), the baseline libraries, and `wsl.conf` (no Windows drives, no Windows programs, non-root user).
- `scripts/build_rootfs.sh`: builds the image and exports `dist/parasync-runner-rootfs-<version>.tar.gz` plus `dist/manifest.json` (SHA-256, size, harness commit).

## How the desktop app uses it
1. Downloads the archive from a GitHub release and checks its SHA-256 against the manifest.
2. `wsl --import ParasyncRunner <folder> <archive>`.
3. For each job: `wsl.exe -d ParasyncRunner -- timeout --signal=KILL <N> python3 /opt/runner/run_job.py`, job JSON on stdin, result line on stdout.

## Develop
```
python3 -m pytest -q tests          # harness tests (library tests skip if libraries are absent)
scripts/build_rootfs.sh             # needs Docker
```
CI runs the tests again inside the built image, checks the sealed-room settings, and uploads the archive. Pushing a `v*` tag publishes a release.

## Versions
Bump `VERSION` when the image or harness changes. Move Python (here and in the cloud sandbox) together, never one.
