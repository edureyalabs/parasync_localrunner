# sandbox/runner.py
"""
Execution of admin- or model-authored Python, in one of two modes:

  - "function" mode: code must define run(...), called with declared inputs as
    keyword arguments. Used by Tool Builder custom tools.
  - "script" mode: code is executed top-level, no wrapper function required. Used by
    the code_executor platform tool (free-form scripts a model writes on the spot). If
    the script sets a variable named `result`, that's returned; the caller falls back
    to captured stdout otherwise.

DELIBERATE PRODUCT DECISION (2026-07-25, recorded in full in design.md's "Addendum —
Full sandbox autonomy"): this sandbox is UNRESTRICTED Python — no import allowlist, no
filesystem restriction, real `pip install`. Earlier versions of this file had an
import allowlist + blocked `open()` + a hand-rolled SSRF-guarded HTTP helper; all of
that was removed on explicit instruction, after the tradeoffs were discussed at
length, because the platform's whole premise is agents that act as real "digital
employees" and that requires real terminal-like capability (arbitrary packages,
arbitrary file I/O, arbitrary network). Hardening (real per-call container isolation,
egress policy, etc.) is an explicitly deferred follow-up phase, not an oversight here.

What's still enforced, and why these are NOT capability restrictions in the same
sense: this is one shared container serving every agent on the platform, so:
  - hard wall-clock timeout + CPU/memory rlimits — stop one agent's runaway/buggy
    script from starving every other agent's execution on the same box
  - a bounded concurrency cap (see main.py) — same reasoning
  - the child process gets an explicit, minimal environment, NOT a copy of this
    service's own os.environ — so code can never read this service's own
    SANDBOX_SECRET out of its environment. Costs zero capability, closes a real leak
    that existed in the reference implementation this was modeled on.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import pathlib
import sys
import tempfile
import time

from config import settings

_RESULT_MARKER = "__PARASYNC_RESULT__"
_HARNESS_FILENAME = "__parasync_harness__.py"

_FUNCTION_HARNESS = '''
import sys, json

# Confirmed live 2026-08-10: this harness runs under `python3 -I` (isolated
# mode, see _run_harness below) — deliberately kept for this shared, reused
# container's own reason ("don't let one execution's environment leak into
# the next"), but `-I` also implies `-E`, which makes Python ignore the
# PYTHONPATH env var entirely. That silently broke every extra package
# installed via the `packages` argument (which lands in a per-agent cache
# dir found ONLY via PYTHONPATH) — `pip install` reports success and the
# files are genuinely on disk, but `import` can never see them, no matter
# how many times the model retries. Baseline packages were never affected
# (they're installed into the image's own global site-packages at build
# time, unaffected by `-I`/`-s`) — this only ever hit non-baseline packages,
# which is why it went unnoticed until an agent's workflow specifically
# needed boto3/pyathena. Injecting the cache dir directly into sys.path here
# (rather than relying on the env var) works regardless of `-I`, and keeps
# every other isolation `-I` provides (PYTHONSTARTUP, user site-packages,
# etc.) intact — this is a narrow, deliberate exception, not a reason to
# reconsider `-I` itself.
if {pkg_dir!r}:
    sys.path.insert(0, {pkg_dir!r})

try:
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, ({cpu_seconds}, {cpu_seconds}))
    resource.setrlimit(resource.RLIMIT_AS, ({max_memory_bytes}, {max_memory_bytes}))
except Exception:
    pass

_inputs = json.loads(sys.stdin.read())
_ns = {{}}

try:
    exec(compile({user_code!r}, "<tool>", "exec"), _ns)
except Exception as exc:
    print("{marker}" + json.dumps({{"ok": False, "error": "tool code failed to load: " + type(exc).__name__ + ": " + str(exc)}}))
    sys.exit(0)

if "run" not in _ns or not callable(_ns["run"]):
    print("{marker}" + json.dumps({{"ok": False, "error": "tool code must define a function named run(...)"}}))
    sys.exit(0)

try:
    _result = _ns["run"](**_inputs)
    json.dumps(_result)  # ensure JSON-serializable before declaring success
    print("{marker}" + json.dumps({{"ok": True, "result": _result}}))
except TypeError as exc:
    print("{marker}" + json.dumps({{"ok": False, "error": "invalid arguments: " + str(exc)}}))
except Exception as exc:
    print("{marker}" + json.dumps({{"ok": False, "error": type(exc).__name__ + ": " + str(exc)}}))
'''

_SCRIPT_HARNESS = '''
import sys, json

# See _FUNCTION_HARNESS's identical block above for the full incident
# writeup — `-I` (isolated mode, see _run_harness below) silently ignores
# PYTHONPATH, which broke every non-baseline `packages` install on this
# backend. Injecting the per-agent cache dir directly into sys.path here
# works regardless of `-I`.
if {pkg_dir!r}:
    sys.path.insert(0, {pkg_dir!r})

try:
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, ({cpu_seconds}, {cpu_seconds}))
    resource.setrlimit(resource.RLIMIT_AS, ({max_memory_bytes}, {max_memory_bytes}))
except Exception:
    pass

_ns = {{}}

try:
    exec(compile({user_code!r}, "<script>", "exec"), _ns)
except Exception as exc:
    print("{marker}" + json.dumps({{"ok": False, "error": type(exc).__name__ + ": " + str(exc)}}))
    sys.exit(0)

if "result" in _ns:
    try:
        json.dumps(_ns["result"])
        print("{marker}" + json.dumps({{"ok": True, "result": _ns["result"]}}))
    except TypeError:
        print("{marker}" + json.dumps({{"ok": False, "error": "`result` is not JSON-serializable"}}))
else:
    print("{marker}" + json.dumps({{"ok": True, "result": None}}))
'''


def _safe_child_env(extra_pythonpath: str | None = None, extra_vars: dict[str, str] | None = None) -> dict:
    """Explicit, minimal environment for the executed subprocess — never a copy of
    this service's own os.environ, so SANDBOX_SECRET (and anything else set on this
    service) is never readable from inside executed code. `extra_vars` is how bemain
    injects resolved, decrypted agent secrets into a single call — scoped to just that
    one subprocess, never persisted, never this service's own environment."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": "/tmp",
        "LANG": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if extra_pythonpath:
        env["PYTHONPATH"] = extra_pythonpath
    if extra_vars:
        env.update(extra_vars)
    return env


def _snapshot(directory: str, exclude: set[str]) -> dict[str, str]:
    snapshot = {}
    if not os.path.isdir(directory):
        return snapshot
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
        for fname in files:
            abs_path = os.path.join(root, fname)
            rel_path = os.path.relpath(abs_path, directory).replace("\\", "/")
            if rel_path in exclude:
                continue
            try:
                with open(abs_path, "rb") as f:
                    snapshot[rel_path] = hashlib.sha256(f.read()).hexdigest()
            except Exception:
                pass
    return snapshot


def install_packages(agent_id: str, packages: list[str]) -> dict:
    """Real `pip install`, cached per-agent so repeat calls from the same agent don't
    reinstall. Persists on this container's disk until it restarts/redeploys —
    deliberate, matches the reference implementation's caching design."""
    if not packages:
        return {"ok": True, "installed": []}

    pkg_dir = os.path.join(settings.PKG_CACHE_DIR, agent_id)
    os.makedirs(pkg_dir, exist_ok=True)

    result = _run_subprocess_sync(
        [sys.executable, "-m", "pip", "install", "--target", pkg_dir, *packages],
        timeout=settings.PIP_INSTALL_TIMEOUT_SECONDS,
    )
    return {
        "ok":        result["returncode"] == 0,
        "installed": packages,
        "stdout":    result["stdout"][-4000:],
        "stderr":    result["stderr"][-4000:],
    }


def _run_subprocess_sync(cmd: list[str], timeout: int) -> dict:
    import subprocess
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "stdout": "", "stderr": f"pip install timed out after {timeout}s"}
    except Exception as exc:
        return {"returncode": -1, "stdout": "", "stderr": str(exc)}


async def execute(
    code: str,
    inputs: dict | None = None,
    mode: str = "function",
    agent_id: str = "unknown",
    files: dict[str, str] | None = None,     # relative path -> base64 content, staged before running
    packages: list[str] | None = None,       # pip packages to install (cached per agent_id)
    env_vars: dict[str, str] | None = None,  # resolved secrets, scoped to this one call
    needs_internet_access: bool = False,     # widens the wall-clock kill timeout — see config.py
) -> dict:
    """Runs `code` in a subprocess. Returns {ok, result|error, stdout, output_files}."""
    install_result = None
    if packages:
        install_result = await asyncio.to_thread(install_packages, agent_id, packages)

    pkg_dir = os.path.join(settings.PKG_CACHE_DIR, agent_id)
    pythonpath = pkg_dir if os.path.isdir(pkg_dir) else None

    if mode == "script":
        harness = _SCRIPT_HARNESS.format(
            cpu_seconds=settings.CPU_LIMIT_SECONDS,
            max_memory_bytes=settings.MEMORY_LIMIT_MB * 1024 * 1024,
            user_code=code,
            marker=_RESULT_MARKER,
            pkg_dir=pythonpath or "",
        )
        stdin_payload = b""
    else:
        harness = _FUNCTION_HARNESS.format(
            cpu_seconds=settings.CPU_LIMIT_SECONDS,
            max_memory_bytes=settings.MEMORY_LIMIT_MB * 1024 * 1024,
            user_code=code,
            marker=_RESULT_MARKER,
            pkg_dir=pythonpath or "",
        )
        stdin_payload = json.dumps(inputs or {}).encode("utf-8")

    timeout_seconds = settings.NETWORK_TIMEOUT_SECONDS if needs_internet_access else settings.TIMEOUT_SECONDS
    result = await _run_harness(harness, stdin_payload, files or {}, pythonpath, env_vars, timeout_seconds)
    if install_result is not None:
        result["packages_installed"] = install_result
    return result


async def _run_harness(
    harness: str,
    stdin_payload: bytes,
    files: dict[str, str],
    pythonpath: str | None,
    env_vars: dict[str, str] | None = None,
    timeout_seconds: int = settings.TIMEOUT_SECONDS,
) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        # Stage any input files (e.g. workspace files bemain fetched server-side)
        for rel_path, b64content in files.items():
            dest = pathlib.Path(tmp) / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(base64.b64decode(b64content))

        harness_path = pathlib.Path(tmp) / _HARNESS_FILENAME
        harness_path.write_text(harness, encoding="utf-8")

        pre_snapshot = _snapshot(tmp, exclude={_HARNESS_FILENAME})

        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(harness_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=tmp,
            env=_safe_child_env(pythonpath, env_vars),
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(stdin_payload),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            # Explicit, actionable wording — this result flows straight back
            # to the model as a tool result (bemain's tools_executor.py ->
            # code_executor.py), so it needs to be unambiguous that nothing
            # completed and that retrying the identical call is unlikely to
            # help, rather than leaving the model to guess or silently retry.
            return {
                "ok": False,
                "error": (
                    f"execution timed out after {timeout_seconds}s and was killed — "
                    "the script did NOT finish and nothing it was doing (including any "
                    "in-progress network call, e.g. an email send) is confirmed to have "
                    "completed. Do not report this as succeeded or partially succeeded. "
                    "If this keeps happening on retry, tell the user directly that this "
                    "operation is taking too long to complete rather than continuing to "
                    "retry the same approach."
                ),
                "stdout": "",
                "output_files": {},
            }

        out_text = stdout.decode("utf-8", errors="replace")
        err_text = stderr.decode("utf-8", errors="replace")

        post_snapshot = _snapshot(tmp, exclude={_HARNESS_FILENAME})
        output_files = {}
        for rel_path, file_hash in post_snapshot.items():
            if pre_snapshot.get(rel_path) == file_hash:
                continue
            try:
                output_files[rel_path] = base64.b64encode((pathlib.Path(tmp) / rel_path).read_bytes()).decode()
            except Exception:
                pass

        idx = out_text.rfind(_RESULT_MARKER)
        if idx == -1:
            return {"ok": False, "error": err_text.strip() or "execution produced no result", "stdout": out_text, "output_files": output_files}

        result_line = out_text[idx + len(_RESULT_MARKER):].strip().splitlines()[0]
        try:
            parsed = json.loads(result_line)
        except (json.JSONDecodeError, IndexError):
            return {"ok": False, "error": "sandbox could not parse execution output", "stdout": out_text, "output_files": output_files}

        parsed["stdout"] = out_text[:idx]
        parsed["output_files"] = output_files
        return parsed
