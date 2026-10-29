# scripts/sync_browser.py
"""
Copies the cloud browser's code into runner/ so a browser on a person's PC does exactly what a browser in the cloud MicroVM does (look, act, the resident
watcher with its network / console / dialog / tab / download records, scrubbing, files handed back once, scripts in their own process):

    python3 scripts/sync_browser.py ../parasync_sandbox          copy the files and record their hashes
    python3 scripts/sync_browser.py --check                      fail if a copy was edited by hand (CI runs this through tests/test_synced_browser.py)

The files are copied byte for byte. They are never edited here: a change belongs in parasync_sandbox/microvm/ and is brought over with this script. The
only code that is this repository's own is runner/browser_host.py (starts Chromium on a loopback port and speaks the app's line protocol).
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
runner = root / "runner"
FILES = ("browser_core.py", "senses.py", "driver.py", "scrub.py", "page_js.py", "browser_harness.py")
RECORD = runner / "SYNCED_BROWSER.json"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check() -> int:
    if not RECORD.is_file():
        print("runner/SYNCED_BROWSER.json is missing: run scripts/sync_browser.py <parasync_sandbox checkout>")
        return 1
    recorded = json.loads(RECORD.read_text(encoding="utf-8")).get("files", {})
    bad = [name for name in FILES if not (runner / name).is_file() or recorded.get(name) != digest(runner / name)]
    if bad:
        print("These synced files differ from what was recorded (edited by hand, or not re-synced): " + ", ".join(bad))
        print("Change them in parasync_sandbox/microvm/ and run: python3 scripts/sync_browser.py ../parasync_sandbox")
        return 1
    return 0


def sync(source: Path) -> int:
    microvm = source / "microvm"
    missing = [name for name in FILES if not (microvm / name).is_file()]
    if missing:
        print(f"{microvm} lacks: {', '.join(missing)}")
        return 1
    for name in FILES:
        (runner / name).write_bytes((microvm / name).read_bytes())
    try:
        commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        dirty = bool(subprocess.check_output(["git", "-C", str(source), "status", "--porcelain", "--", "microvm"], text=True).strip())
    except Exception:
        commit, dirty = "unknown", False
    RECORD.write_text(json.dumps({
        "source": "parasync_sandbox/microvm", "commit": commit + ("+uncommitted" if dirty else ""),
        "note": "Verbatim copies. Never edit them here. Re-sync with: python3 scripts/sync_browser.py ../parasync_sandbox",
        "files": {name: digest(runner / name) for name in FILES},
    }, indent=2) + "\n", encoding="utf-8")
    print(f"Copied {len(FILES)} files from {microvm} ({commit[:7]}{'+uncommitted' if dirty else ''}).")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--check":
        sys.exit(check())
    if len(sys.argv) != 2:
        sys.exit("Usage: python3 scripts/sync_browser.py <path to parasync_sandbox>   |   --check")
    sys.exit(sync(Path(sys.argv[1]).resolve()))
