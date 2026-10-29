# tests/test_synced_browser.py
"""The cloud's browser code is copied into runner/ byte for byte (scripts/sync_browser.py) and never edited here: a copy that was edited by hand, or a file added
to the cloud but not copied, fails this test."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_the_copies_match_what_was_recorded_at_the_last_sync():
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "sync_browser.py"), "--check"], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout


def test_the_image_gets_every_file_the_host_needs():
    text = (ROOT / "scripts" / "sync_browser.py").read_text(encoding="utf-8")
    names = [n for n in ("browser_core.py", "senses.py", "driver.py", "scrub.py", "page_js.py", "browser_harness.py") if n in text]
    assert len(names) == 6 and all((ROOT / "runner" / n).is_file() for n in names)
    assert "COPY runner/ /opt/runner/" in (ROOT / "image" / "Dockerfile").read_text(encoding="utf-8")
