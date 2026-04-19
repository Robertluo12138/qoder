#!/usr/bin/env python3
"""Prepare an optional isolated git context for repeated self-supervisor runs."""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent


def utc_stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")


def run_git(args: List[str]) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", "git not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", "git timed out after 120s"


def current_branch() -> str:
    code, out, _ = run_git(["rev-parse", "--abbrev-ref", "HEAD"])
    if code == 0 and out.strip():
        return out.strip()
    return "main"


def default_branch_name(prefix: str) -> str:
    safe = prefix.strip().replace(" ", "-").replace("_", "-") or "qoder"
    return f"{safe}/{utc_stamp()}"


def default_worktree_path(branch: str) -> Path:
    safe = branch.replace("/", "-")
    return REPO_ROOT.parent / f"{REPO_ROOT.name}-{safe}"


def build_plan(mode: str, branch: str, base: str, worktree_path: Path | None) -> Dict[str, Any]:
    if mode == "branch":
        commands = [["git", "switch", "-c", branch, base]]
        target_path = REPO_ROOT
    else:
        target_path = worktree_path or default_worktree_path(branch)
        commands = [["git", "worktree", "add", "-b", branch, str(target_path), base]]

    return {
        "mode": mode,
        "repo_root": str(REPO_ROOT),
        "base_ref": base,
        "branch": branch,
        "target_path": str(target_path),
        "commands": commands,
        "recommended_next_steps": [
            f"cd {target_path}",
            "python3 scripts/clean_state.py",
            "python3 scripts/preflight.py --json",
            "python3 scripts/run_tests.py",
            "Invoke the Qoder project command `self-supervisor` from that checkout.",
        ],
    }


def apply_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    executions: List[Dict[str, Any]] = []
    for cmd in plan["commands"]:
        code, out, err = run_git(cmd[1:])
        executions.append(
            {
                "command": cmd,
                "exit_code": code,
                "stdout": out,
                "stderr": err,
            }
        )
        if code != 0:
            return {"ok": False, "executions": executions}
    return {"ok": True, "executions": executions}


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Prepare an isolated git context for a self-supervisor run.")
    parser.add_argument("--mode", choices=("branch", "worktree"), default="worktree")
    parser.add_argument("--branch", default=None, help="branch name to create")
    parser.add_argument("--base", default="HEAD", help="base ref for the new branch/worktree")
    parser.add_argument("--path", default=None, help="target path for worktree mode")
    parser.add_argument("--prefix", default="qoder-self-supervisor", help="prefix for generated branch names")
    parser.add_argument("--apply", action="store_true", help="execute the git command instead of previewing it")
    parser.add_argument("--json", action="store_true", help="emit JSON output")
    args = parser.parse_args(argv)

    code, out, err = run_git(["rev-parse", "--is-inside-work-tree"])
    if code != 0 or out.strip() != "true":
        payload = {
            "ok": False,
            "error": "not_a_git_repo",
            "stderr": err.strip(),
        }
        print(json.dumps(payload, indent=2))
        return 1

    branch = args.branch or default_branch_name(args.prefix)
    plan = build_plan(
        args.mode,
        branch,
        args.base,
        Path(args.path).expanduser().resolve() if args.path else None,
    )

    summary: Dict[str, Any] = {
        "ok": True,
        "current_branch": current_branch(),
        "plan": plan,
        "applied": False,
    }

    if args.apply:
        result = apply_plan(plan)
        summary["applied"] = True
        summary["ok"] = bool(result["ok"])
        summary["executions"] = result["executions"]

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        state = "apply" if args.apply else "preview"
        print(f"[isolation] mode={plan['mode']} ({state})")
        print(f"[isolation] current_branch={summary['current_branch']}")
        print(f"[isolation] target_branch={plan['branch']}")
        print(f"[isolation] target_path={plan['target_path']}")
        for cmd in plan["commands"]:
            print(f"[isolation] command: {' '.join(cmd)}")
        if args.apply and not summary["ok"]:
            print("[isolation] command failed")
        else:
            print("[isolation] next steps:")
            for item in plan["recommended_next_steps"]:
                print(f"  - {item}")

    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
