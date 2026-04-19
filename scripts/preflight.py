#!/usr/bin/env python3
"""Preflight checks for the Qoder-native self-supervisor workflow."""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from qoder_invoke import probe_qodercli  # noqa: E402


DEFAULT_IGNORE: List[str] = [
    "__pycache__",
    "*.pyc",
    "*.pyo",
    ".venv",
    "venv",
    "artifacts",
    ".qoder/state",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".DS_Store",
]


def load_config(root: Path) -> Dict[str, Any]:
    cfg_path = root / "supervisor_config.json"
    if not cfg_path.exists():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"__error__": f"failed to load supervisor_config.json: {exc}"}


def run_git(args: List[str], cwd: Path) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", "git: command not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", "git: command timed out after 30s"


def check_is_git(root: Path) -> Dict[str, Any]:
    code, out, err = run_git(["rev-parse", "--is-inside-work-tree"], root)
    if code == 0 and out.strip() == "true":
        top_code, top_out, top_err = run_git(["rev-parse", "--show-toplevel"], root)
        return {
            "ok": top_code == 0,
            "git_root": top_out.strip() if top_code == 0 else None,
            "stderr": top_err.strip(),
        }
    return {"ok": False, "git_root": None, "stderr": err.strip()}


def check_project_root(root: Path) -> Dict[str, Any]:
    markers = [
        root / ".git",
        root / "supervisor_config.json",
        root / ".qoder",
    ]
    found = [str(marker.relative_to(root)) for marker in markers if marker.exists()]
    return {"ok": bool(found), "markers": found}


def is_ignored(rel_path: str, patterns: List[str]) -> bool:
    if not patterns:
        return False
    parts = rel_path.split("/")
    for pat in patterns:
        if fnmatch.fnmatch(rel_path, pat):
            return True
        for part in parts:
            if fnmatch.fnmatch(part, pat):
                return True
        if "/" in pat and rel_path.startswith(pat.rstrip("/") + "/"):
            return True
    return False


def collect_dirty(root: Path, ignore: List[str]) -> List[Dict[str, str]]:
    code, out, _err = run_git(["status", "--porcelain"], root)
    if code != 0:
        return []
    dirty: List[Dict[str, str]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        status = line[:2]
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip('"')
        if is_ignored(path, ignore):
            continue
        dirty.append({"status": status, "path": path})
    return dirty


def check_python() -> Dict[str, Any]:
    return {
        "ok": True,
        "version": sys.version.split()[0],
        "executable": sys.executable,
    }


def run_unified_tests(root: Path) -> Dict[str, Any]:
    cmd = [sys.executable, str(root / "scripts" / "run_tests.py")]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "runner_command": cmd,
            "exit_code": 124,
            "status": "timeout",
        }
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "runner_command": cmd,
            "exit_code": proc.returncode,
            "status": "invalid_json",
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    payload["runner_command"] = cmd
    payload["ok"] = bool(payload.get("passed"))
    return payload


def apply_fix(root: Path, ignore: List[str]) -> List[str]:
    actions: List[str] = []

    artifacts = root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    gitkeep = artifacts / ".gitkeep"
    if not gitkeep.exists():
        gitkeep.touch()
        actions.append("created artifacts/.gitkeep")

    for directory in (
        root / ".qoder" / "commands",
        root / ".qoder" / "skills" / "self-supervisor-v1",
        root / ".qoder" / "state" / "tasks",
    ):
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            actions.append(f"created {directory.relative_to(root)}/")

    if not (root / ".git").exists():
        code, _out, _err = run_git(["init", "-b", "main"], root)
        if code != 0:
            code2, _out2, _err2 = run_git(["init"], root)
            if code2 == 0:
                actions.append("initialized git repository")
        else:
            actions.append("initialized git repository on main")

    gitignore = root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
    additions: List[str] = []
    for pat in ignore:
        if pat == "artifacts":
            continue
        entry = pat
        if "/" not in pat and "*" not in pat and "." not in pat:
            entry = pat + "/"
        if entry not in existing and entry not in additions:
            additions.append(entry)
    for line in ("artifacts/*", "!artifacts/.gitkeep"):
        if line not in existing and line not in additions:
            additions.append(line)
    if additions:
        with gitignore.open("a", encoding="utf-8") as handle:
            if existing and existing[-1].strip():
                handle.write("\n")
            handle.write("# appended by scripts/preflight.py --fix\n")
            for line in additions:
                handle.write(line + "\n")
        actions.append(f"appended {len(additions)} ignore entries to .gitignore")

    return actions


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Preflight for the Qoder-native workflow.")
    parser.add_argument("--fix", action="store_true", help="apply safe remediations")
    parser.add_argument("--json", action="store_true", help="emit JSON output")
    args = parser.parse_args(argv)

    root = REPO_ROOT
    cfg = load_config(root)
    ignore = list(cfg.get("ignore_paths") or DEFAULT_IGNORE)
    allow_dirty = bool(cfg.get("allow_dirty_repo", False))

    report: Dict[str, Any] = {
        "schema_version": 2,
        "root": str(root),
        "config_loaded": "__error__" not in cfg,
        "checks": {},
    }
    if "__error__" in cfg:
        report["config_error"] = cfg["__error__"]

    if args.fix:
        report["fix"] = {"actions": apply_fix(root, ignore)}

    project_root = check_project_root(root)
    git = check_is_git(root)
    python = check_python()
    qoder = probe_qodercli(root)
    dirty = collect_dirty(root, ignore) if git["ok"] else []
    tests = run_unified_tests(root)

    report["checks"]["project_root"] = project_root
    report["checks"]["git"] = git
    report["checks"]["python"] = python
    report["checks"]["qodercli"] = qoder
    report["checks"]["unified_test_entry"] = tests
    report["checks"]["dirty_files"] = {
        "count": len(dirty),
        "entries": dirty,
        "ignored_patterns": ignore,
    }

    blocking: List[str] = []
    warnings: List[str] = []

    if not project_root["ok"]:
        blocking.append("project_root_not_recognized")
    if not git["ok"]:
        blocking.append("not_a_git_repo")
    if not python["ok"]:
        blocking.append("python_unavailable")
    if not qoder.get("binary_found"):
        blocking.append("qodercli_not_callable")
    else:
        if not qoder["help"].get("ok"):
            blocking.append("qodercli_help_probe_failed")
        if not qoder["version"].get("ok"):
            blocking.append("qodercli_version_probe_failed")
        if not qoder["headless_probe"].get("ok"):
            blocking.append("qodercli_headless_probe_failed")
    if not tests.get("ok"):
        blocking.append("unified_test_entry_failed")
    if dirty and not allow_dirty:
        blocking.append("dirty_repo")
    if dirty and allow_dirty:
        warnings.append("dirty_repo_allowed_by_config")

    report["blocking"] = blocking
    report["warnings"] = warnings
    report["ready"] = not blocking

    print(json.dumps(report, indent=2))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
