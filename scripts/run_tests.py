#!/usr/bin/env python3
"""Unified test entry point for the Qoder self-supervisor workflow.

This is the **only** test command used anywhere in the workflow. Every
stage — orchestrator, verify_delivery — shells out to this script so
test evidence is consistent across runs.

Behavior:

- reads ``supervisor_config.json`` for the ``test_command`` override;
- defaults to ``python -m pytest -q`` when nothing is configured;
- prefers ``.venv/bin/python`` over the ambient ``python`` when
  ``prefer_repo_venv`` is true and a venv exists;
- treats pytest's ``exit 5`` (no tests collected) as a *pass*, exposed
  as ``status: "ok_no_tests"``. This lets early-stage projects succeed
  before they have any tests yet.

JSON schema (stdout):

    {
      "schema_version": 1,
      "command": [...],
      "status": "ok" | "ok_no_tests" | "failed" | "error" | "timeout",
      "passed": bool,
      "exit_code": int,
      "returncode": int,
      "stdout": str,
      "stderr": str,
      "duration_s": float,
      "timed_out": bool
    }

Exit code is ``0`` on pass (including ``ok_no_tests``) and ``1`` on any
failure. This mirrors the ``passed`` flag in the JSON payload.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List


def repo_root_from_script() -> Path:
    return Path(__file__).resolve().parent.parent


def load_config(root: Path) -> Dict[str, Any]:
    cfg_path = root / "supervisor_config.json"
    if not cfg_path.exists():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def resolve_command(cfg: Dict[str, Any]) -> List[str]:
    """Return the argv for the test command."""
    default = ["python", "-m", "pytest", "-q"]
    cmd = cfg.get("test_command")
    if not cmd:
        return default
    if isinstance(cmd, list):
        return [str(x) for x in cmd]
    if isinstance(cmd, str):
        return shlex.split(cmd)
    return default


def prefer_venv_python(root: Path, cmd: List[str], prefer: bool) -> List[str]:
    """Swap the leading ``python`` token with ``.venv/bin/python`` when asked."""
    if not prefer or not cmd or cmd[0] != "python":
        return cmd
    # Both POSIX and Windows-ish layouts.
    candidates = [
        root / ".venv" / "bin" / "python",
        root / ".venv" / "bin" / "python3",
        root / ".venv" / "Scripts" / "python.exe",
    ]
    for cand in candidates:
        if cand.exists():
            return [str(cand)] + cmd[1:]
    return cmd


def classify(exit_code: int) -> str:
    """Map exit code → string status. Based on pytest's documented codes."""
    # pytest exit codes:
    #   0 = passed
    #   1 = tests failed
    #   2 = usage error
    #   3 = internal error
    #   4 = command line error
    #   5 = no tests collected
    if exit_code == 0:
        return "ok"
    if exit_code == 5:
        return "ok_no_tests"
    if exit_code == 1:
        return "failed"
    return "error"


def run(cmd: List[str], cwd: Path, timeout: int) -> Dict[str, Any]:
    start = time.monotonic()
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return {
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "duration_s": round(time.monotonic() - start, 3),
            "timed_out": False,
        }
    except FileNotFoundError as exc:
        return {
            "exit_code": 127,
            "stdout": "",
            "stderr": f"command not found: {exc}",
            "duration_s": round(time.monotonic() - start, 3),
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
        return {
            "exit_code": 124,
            "stdout": out or "",
            "stderr": f"test command timed out after {timeout}s",
            "duration_s": round(time.monotonic() - start, 3),
            "timed_out": True,
        }


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Unified test runner.")
    parser.add_argument("--json", action="store_true", help="emit JSON (default)")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print captured stdout/stderr to stderr after the JSON payload",
    )
    parser.add_argument(
        "--timeout", type=int, default=600, help="subprocess timeout in seconds"
    )
    parser.add_argument(
        "--command",
        default=None,
        help="override test command (single shell-quoted string)",
    )
    args = parser.parse_args(argv)

    root = repo_root_from_script()
    cfg = load_config(root)
    cmd = shlex.split(args.command) if args.command else resolve_command(cfg)
    cmd = prefer_venv_python(root, cmd, bool(cfg.get("prefer_repo_venv", False)))

    result = run(cmd, root, args.timeout)
    status = "timeout" if result["timed_out"] else classify(result["exit_code"])

    payload: Dict[str, Any] = {
        "schema_version": 1,
        "command": cmd,
        "status": status,
        "passed": status in ("ok", "ok_no_tests"),
        "exit_code": result["exit_code"],
        "returncode": result["exit_code"],
        "stdout": result["stdout"],
        "stderr": result["stderr"],
        "duration_s": result["duration_s"],
        "timed_out": result["timed_out"],
    }

    print(json.dumps(payload, indent=2))
    if args.verbose:
        if result["stdout"]:
            sys.stderr.write("\n--- stdout ---\n")
            sys.stderr.write(result["stdout"])
        if result["stderr"]:
            sys.stderr.write("\n--- stderr ---\n")
            sys.stderr.write(result["stderr"])

    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
