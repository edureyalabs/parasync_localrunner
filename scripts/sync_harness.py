# scripts/sync_harness.py
"""
Copies the cloud sandbox's runner.py into runner/ so the PC runs the very same code as the cloud. Never edit runner/runner.py by hand.

    python3 scripts/sync_harness.py ../parasync_sandbox

It also checks that every `settings.<NAME>` runner.py reads exists in runner/config.py (a new limit added in the cloud would otherwise
fail only at run time), and records which cloud commit the copy came from.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
source = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
if source is None or not (source / "runner.py").is_file():
    sys.exit("Usage: python3 scripts/sync_harness.py <path to the parasync_sandbox checkout>")

text = (source / "runner.py").read_text(encoding="utf-8")
needed = set(re.findall(r"settings\.([A-Z_]+)", text))
have = set(re.findall(r"^\s{4}([A-Z_]+):", (root / "runner" / "config.py").read_text(encoding="utf-8"), re.M))
missing = sorted(needed - have)
if missing:
    sys.exit(f"runner.py reads settings the local config does not define: {', '.join(missing)}. Add them to runner/config.py first.")

(root / "runner" / "runner.py").write_text(text, encoding="utf-8")
try:
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
except Exception:
    commit = "unknown"
(root / "runner" / "SYNCED_FROM.json").write_text(json.dumps({
    "source": "parasync_sandbox/runner.py", "commit": commit,
    "note": "runner.py is a verbatim copy. Re-sync with: python3 scripts/sync_harness.py ../parasync_sandbox",
}, indent=2) + "\n", encoding="utf-8")
print(f"Copied runner.py from {source} ({commit[:7]}).")
