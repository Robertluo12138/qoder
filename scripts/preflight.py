#!/usr/bin/env python3
"""Preflight checks for the Qoder self-supervisor workflow.

Verifies the environment is ready to run the orchestrator:

  - the current directory is a git repository or otherwise recognized
    project root (e.g. contains ``supervisor_config.json``);
  - ``python3`` is available (this script is already running under it);
  - the ``qoder`` CLI is callable (soft requirement — reported but not
    blocking, because the orchestrator degrades gracefully);
  - the repo is clean, ignoring temporary paths such as ``__pycache__``,
    ``*.pyc``, ``.venv``, ``artifacts/``, and ``.qoder/state/``.

The tool emits a **structured JSON report** on stdout so the orchestrator
can consume it mechanically. Exit code is ``0`` when ready to proceed,
``1`` when at least one blocking issue is detected.

``--fix`` applies the conservative remediations we can safely make:

  * create ``artifacts/`` and ``.qoder/state/tasks/`` if missing;
  * append sensible ignore patterns to ``.gitignore``;
  * ``git init`` if the directory is not already a repo.

The script depends only on the Python standard library so it works
before dependencies are installed.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# Keep this list in sync with ``supervisor_config.json``; it is used as a
# fallback when the config is missing.
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


def repo_root_from_script() -> Path:
    """Return the repo root, assuming this script lives in ``scripts/``."""
    return Path(__file__).resolve().parent.parent


def load_config(root: Path) -> Dict[str, Any]:
    """Load ``supervisor_config.json``. Returns ``{}`` (or a sentinel error
    dict) on failure so the caller never crashes on malformed config."""
    cfg_path = root / "supervisor_config.json"
    if not cfg_path.exists():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"__error__": f"failed to load supervisor_config.json: {exc}"}


def which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)


def run_git(args: List[str], cwd: Path) -> Tuple[int, str, str]:
    """Run ``git <args>`` with a short timeout; returns (code, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", "git: command not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", "git: command timed out after 30s"


def is_ignored(rel_path: str, patterns: List[str]) -> bool:
    """Match ``rel_path`` (repo-relative, forward-slash) against each pattern.

    Patterns may be:

    - a fnmatch glob (``*.pyc``, ``__pycache__``),
    - a multi-segment path (``.qoder/state``), which also matches any file
      beneath that directory.
    """
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


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_is_git(root: Path) -> Dict[str, Any]:
    """Is ``root`` inside a git working tree?"""
    code, out, err = run_git(["rev-parse", "--is-inside-work-tree"], root)
    if code == 0 and out.strip() == "true":
        _, top_out, _ = run_git(["rev-parse", "--show-toplevel"], root)
        return {"ok": True, "git_root": top_out.strip() or str(root)}
    return {"ok": False, "git_root": None, "stderr": err.strip()}


def collect_dirty(root: Path, ignore: List[str]) -> List[Dict[str, str]]:
    """Return a list of dirty file entries after filtering through ``ignore``."""
    code, out, _err = run_git(["status", "--porcelain"], root)
    if code != 0:
        return []
    dirty: List[Dict[str, str]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        # Porcelain: ``XY path`` or ``XY old -> new``.
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


def check_qoder(cli_name: str) -> Dict[str, Any]:
    """Probe the qoder CLI. Soft check: reported but never blocking."""
    path = which(cli_name)
    if not path:
        return {"ok": False, "path": None, "note": f"{cli_name!r} not on PATH"}
    # Prefer ``--version``; fall back to ``--help``.
    for probe in (["--version"], ["-V"], ["--help"]):
        try:
            proc = subprocess.run(
                [cli_name, *probe], capture_output=True, text=True, timeout=10
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "path": path, "note": "qoder probe timed out"}
        except OSError as exc:
            return {"ok": False, "path": path, "note": f"qoder probe failed: {exc}"}
        if proc.returncode == 0:
            head = (proc.stdout or proc.stderr).strip().splitlines()
            return {
                "ok": True,
                "path": path,
                "probe": probe[0],
                "stdout_head": (head[0] if head else "")[:200],
            }
    return {
        "ok": False,
        "path": path,
        "note": "qoder responded non-zero to --version, -V, and --help",
    }


def check_project_root(root: Path) -> Dict[str, Any]:
    """Is this a recognized project root?"""
    markers = [
        root / ".git",
        root / "supervisor_config.json",
        root / "pyproject.toml",
        root / "package.json",
    ]
    found = [str(m.relative_to(root)) for m in markers if m.exists()]
    return {"ok": bool(found), "markers": found}


# ---------------------------------------------------------------------------
# --fix
# ---------------------------------------------------------------------------

def apply_fix(root: Path, ignore: List[str]) -> List[str]:
    """Apply safe, idempotent remediations. Returns a log of actions taken."""
    actions: List[str] = []

    artifacts = root / "artifacts"
    if not artifacts.exists():
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / ".gitkeep").touch()
        actions.append("created artifacts/")

    tasks = root / ".qoder" / "state" / "tasks"
    if not tasks.exists():
        tasks.mkdir(parents=True, exist_ok=True)
        actions.append("created .qoder/state/tasks/")

    gitignore = root / ".gitignore"
    existing: List[str] = []
    if gitignore.exists():
        existing = gitignore.read_text(encoding="utf-8").splitlines()
    # Build candidate entries: from ignore_paths (except artifacts/ which
    # deserves a special block so .gitkeep remains tracked).
    candidates: List[str] = []
    for pat in ignore:
        # Normalise: directory-like entries should end with ``/`` when written
        # to .gitignore, to match git's directory semantics.
        if pat == "artifacts":
            continue
        entry = pat
        if "/" not in pat and "." not in pat and "*" not in pat:
            entry = pat + "/"
        if entry not in existing and entry not in candidates:
            candidates.append(entry)
    # Artifacts block
    for line in ("artifacts/*", "!artifacts/.gitkeep"):
        if line not in existing and line not in candidates:
            candidates.append(line)
    if candidates:
        with gitignore.open("a", encoding="utf-8") as fh:
            if existing and existing[-1].strip():
                fh.write("\n")
            fh.write("# appended by scripts/preflight.py --fix\n")
            for line in candidates:
                fh.write(line + "\n")
        actions.append(f"appended {len(candidates)} entries to .gitignore")

    if not (root / ".git").exists():
        # Prefer -b main on modern git; fall back to plain ``git init``.
        code, _, err = run_git(["init", "-b", "main"], root)
        if code != 0:
            code2, _, err2 = run_git(["init"], root)
            if code2 != 0:
                actions.append(f"git init failed: {err.strip() or err2.strip()}")
            else:
                actions.append("initialized git repo (default branch)")
        else:
            actions.append("initialized git repo (branch main)")

    return actions


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Preflight for the Qoder self-supervisor workflow.",
    )
    parser.add_argument("--fix", action="store_true", help="apply safe remediations")
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit JSON (default; flag retained for clarity)",
    )
    parser.add_argument("--root", default=None, help="override repo root")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else repo_root_from_script()
    if not root.exists():
        print(json.dumps({"ready": False, "error": f"root does not exist: {root}"}))
        return 1

    cfg = load_config(root)
    ignore: List[str] = list(cfg.get("ignore_paths") or DEFAULT_IGNORE)
    qoder_cli: str = str(cfg.get("qoder_cli", "qoder"))
    allow_dirty: bool = bool(cfg.get("allow_dirty_repo", False))

    report: Dict[str, Any] = {
        "schema_version": 1,
        "root": str(root),
        "config_loaded": "__error__" not in cfg,
        "checks": {},
    }
    if "__error__" in cfg:
        report["config_error"] = cfg["__error__"]

    # Pre-fix checks
    project = check_project_root(root)
    git = check_is_git(root)
    python = check_python()
    qoder = check_qoder(qoder_cli)
    dirty: List[Dict[str, str]] = collect_dirty(root, ignore) if git["ok"] else []

    report["checks"]["project_root"] = project
    report["checks"]["git"] = git
    report["checks"]["python"] = python
    report["checks"]["qoder"] = qoder
    report["checks"]["dirty_files"] = {
        "count": len(dirty),
        "entries": dirty,
        "ignored_patterns": ignore,
    }

    if args.fix:
        actions = apply_fix(root, ignore)
        # Re-evaluate the checks that --fix can influence.
        project = check_project_root(root)
        git = check_is_git(root)
        dirty = collect_dirty(root, ignore) if git["ok"] else []
        report["fix"] = {"actions": actions}
        report["checks"]["project_root"] = project
        report["checks"]["git"] = git
        report["checks"]["dirty_files"] = {
            "count": len(dirty),
            "entries": dirty,
            "ignored_patterns": ignore,
        }

    # Compute blocking issues.
    blocking: List[str] = []
    if not project["ok"]:
        blocking.append("project_root_not_recognized")
    if not python["ok"]:
        blocking.append("python_unavailable")
    if not git["ok"]:
        blocking.append("not_a_git_repo")
    if dirty and not allow_dirty:
        blocking.append("dirty_repo")

    warnings: List[str] = []
    if not qoder["ok"]:
        warnings.append("qoder_cli_not_callable")

    report["blocking"] = blocking
    report["warnings"] = warnings
    report["ready"] = not blocking

    print(json.dumps(report, indent=2))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
