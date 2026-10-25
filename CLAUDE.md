<!-- CLAUDE.md -->
# Parasync local runner — project instructions

## Rules
- File path as a comment on the first line of every file (`# path`, `<!-- path -->`). JSON files cannot carry it.
- Never edit `runner/runner.py`; run `python3 scripts/sync_harness.py` to refresh it from the cloud sandbox.
- No secrets here, ever. This environment never holds Supabase or provider keys; anything privileged stays in the cloud.
- The sealed-room settings in `image/wsl.conf` (no automount, no interop, non-root user) are a security boundary; do not loosen them.
- Keep Python at the same version as the cloud sandbox.
- Run `python3 -m pytest -q tests` before saying a task is done. Ask before adding dependencies.

**Four repos ship together** (frontmain, bemain, desktop, localrunner): a change to any shared contract needs the matching change in the others, released in order migration -> bemain/frontmain -> localrunner -> desktop, and old/new must degrade to the cloud. Rules: `parasync_desktop/docs/LOCAL_EXECUTION.md` section 0.
