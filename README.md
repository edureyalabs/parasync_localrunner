<!-- README.md -->
# Parasync local runner

The Linux environment that runs agent tool code **and browser sessions** on a person's own Windows PC, instead of in the cloud.
The whole system (all four repos, routing, trust rules, releases, debugging) is described in `parasync_desktop/docs/LOCAL_EXECUTION.md`; read that first.

## What it is
- `runner/runner.py`: a verbatim copy of the cloud sandbox's harness (never edit by hand; `scripts/sync_harness.py` refreshes it and records the source commit in `runner/SYNCED_FROM.json`).
- `runner/run_job.py`: the program the desktop app starts per code run. It reads one JSON job on stdin, runs it through the harness, and prints exactly one `__PARASYNC_JOB_RESULT__{json}` line on stdout.
- `runner/browser_host.py` (image 0.2.0+): one process per browser session. Headless Chromium through synchronous Playwright (in-memory profile, no network port), driven by the app over stdin/stdout JSON lines
  (`exec` / `ping` / `close` in, `__PARASYNC_BROWSER__{json}` lines out). Scripts see `browser`, `context`, `page` like the cloud harness.
- `runner/job_supervisor.py` + `runner/local_broker.py` (image 0.3.0+): how a job that was given secrets is started. The supervisor stays root, keeps the secrets in its own memory,
  runs the broker on 127.0.0.1 and starts the script as the unprivileged user with only the broker's address and a one-time token. The script can ask the broker to make an https request
  with a secret in it (only to that secret's own hosts, only to public addresses) and gets the answer back with the value blanked; it can never read the value. See `parasync_desktop/docs/LOCAL_SECRETS.md`.
- `image/`: the Dockerfile (Python 3.9 to match the cloud; Playwright 1.49.1 and Chromium), the baseline libraries, and `wsl.conf` (no Windows drives, no Windows programs, non-root user).
- `scripts/build_rootfs.sh`: builds the image and exports `dist/parasync-runner-rootfs-<version>.tar.gz` plus `dist/manifest.json` (SHA-256, size, harness commit).

## How the desktop app uses it
1. Downloads the archive from the latest GitHub release and checks its SHA-256 against the manifest; `wsl --import ParasyncRunner <folder> <archive>`. Later releases replace it automatically when the PC is idle.
2. Every job and every browser host starts as root only to lock Windows interop (`wsl.exe` leaves it on whatever `wsl.conf` says), then drops to the unprivileged `runner` user before any agent code exists.
3. A code run: `... runuser -u runner -- env -u WSL_INTEROP timeout --signal=KILL <N> python3 /opt/runner/run_job.py`, job JSON on stdin, result line on stdout.
4. A browser session: `... python3 /opt/runner/browser_host.py <session id>`, kept alive by the app for the session's life.

## Develop
```
python3 -m pytest -q tests          # harness + browser-host tests (library / real-browser tests skip when the libraries are absent)
scripts/build_rootfs.sh             # needs Docker
```
CI runs the tests again inside the built image (including a real Chromium session), checks the sealed-room settings, and uploads the archive.

## Releasing a new image
Bump `VERSION`, push to `main`, wait for CI to go green, then on GitHub create a release `vX.Y.Z` (create the tag on publish, set as latest). CI's tag run attaches `manifest.json` and the rootfs.
The repository must stay public (the app downloads without credentials). Installed apps pick the new image up themselves (checked every 6 h, applied when idle).

## Versions
Bump `VERSION` when the image or harness changes. Move Python (here and in the cloud sandbox) together, never one. 0.1.0: code runs. 0.2.0: browsers. 0.3.0: the local broker (secrets kept away from the script).
