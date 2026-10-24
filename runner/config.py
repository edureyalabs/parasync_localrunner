# runner/config.py
"""
Limits for code run on a person's PC. These are the SAME numbers and names the cloud sandbox uses (parasync_sandbox/config.py),
so a script behaves identically in both places; runner.py (a verbatim copy of the cloud's, see scripts/sync_harness.py) reads
`settings.<NAME>`. The cloud's file needs a shared secret and a web framework; here the only caller is the desktop app on the same
PC, through a pipe (see run_job.py), so this is plain Python with optional environment overrides (PARASYNC_RUNNER_<NAME>), which the
tests use to shorten the timeouts.
"""
from __future__ import annotations

import os

_PREFIX = "PARASYNC_RUNNER_"


def _int(name: str, default: int) -> int:
    raw = os.environ.get(_PREFIX + name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


class Settings:
    # Wall-clock ceilings: a maximum, never a fixed wait (the cloud's values).
    TIMEOUT_SECONDS: int = _int("TIMEOUT_SECONDS", 400)
    NETWORK_TIMEOUT_SECONDS: int = _int("NETWORK_TIMEOUT_SECONDS", 90)
    PIP_INSTALL_TIMEOUT_SECONDS: int = _int("PIP_INSTALL_TIMEOUT_SECONDS", 120)
    # RLIMIT_CPU / RLIMIT_AS inside the script's own process (Linux), same as the cloud.
    CPU_LIMIT_SECONDS: int = _int("CPU_LIMIT_SECONDS", 25)
    MEMORY_LIMIT_MB: int = _int("MEMORY_LIMIT_MB", 768)
    # Packages a script asks for are installed here, per agent, and kept until the Linux box restarts.
    PKG_CACHE_DIR: str = os.environ.get(_PREFIX + "PKG_CACHE_DIR", "/tmp/parasync/packages")


settings = Settings()
