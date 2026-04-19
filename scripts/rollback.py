#!/usr/bin/env python3
"""Restore the working tree to the checkpoint captured before a write.

The orchestrator writes ``.qoder/state/checkpoint.json`` immediately
before invoking the write stage. The checkpoint records:

  - the current ``HEAD`` commit sha,
  - the current branch (or ``"DETACHED"`` if detached),
  - whether the working tree was already dirty when the run started,
  - the list of paths the orchestrator considered "in scope".

Rollback uses this record to bring the tree back to that exact state.

Two scopes are available:

  - ``--scope=changed`` (default): undo only the files the most recent
    delivery report claims it touched. Safer; will not disturb work
    that lives outside the orchestrator's scope.
  - ``--scope=hard``: ``git reset --hard <sha>`` followed by
    ``git clean -fd`` of the configured ``allowed_write_roots``. Use
    this when the write stage corrupted the tree badly.

Both scopes refuse to run unless ``--yes`` is passed, since they are
destructive.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent
STATE = REPO_ROOT / ".qoder" / "state"
CHECKPOINT = STATE / "checkpoint.json"
DELIVERY_REPORT = REPO_ROOT / "artifacts" / "delivery_report.json"


def _git(args: List[str]) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(REPO_ROOT),
            capture_output=True, text=True, timeout=60,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", "git not on PATH"


def _load_json(p: Path) -> Dict[str, Any]:
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _changed_files() -> List[str]:
    """Files the latest delivery's review stage recorded as changed.

    This is intentionally narrow: ``--scope=changed`` exists to undo
    the most recent orchestrator write, not to revert anything else
    that diverges from the checkpoint commit. Using ``git diff <sha>``
    here would also capture any pre-existing dirty state from before
    the run (e.g., the user's in-progress work), which would be a
    silent destructive surprise.

    Returns ``[]`` if no report exists or the review stage is empty —
    callers should treat that as "nothing this script can safely undo;
    use ``--scope=hard`` if you really need a full reset."
    """
    rep = _load_json(DELIVERY_REPORT)
    review = (rep.get("stages") or {}).get("review") or {}
    return list(review.get("changed") or [])


def _allowed_roots() -> List[str]:
    rep = _load_json(DELIVERY_REPORT)
    roots = (rep.get("config_used") or {}).get("allowed_write_roots") or []
    return list(roots)


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Restore the working tree to the pre-write checkpoint.",
    )
    parser.add_argument(
        "--scope", choices=("changed", "hard"), default="changed",
        help="changed: revert only files the report touched (default); "
             "hard: reset --hard + clean -fd of allowed_write_roots",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="confirm the destructive action; required",
    )
    parser.add_argument(
        "--allow-dirty-baseline",
        action="store_true",
        help="permit --scope=hard even when the checkpoint recorded "
             "was_dirty=true (would otherwise refuse, since hard reset "
             "would also discard your pre-existing in-progress work)",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    cp = _load_json(CHECKPOINT)
    if not cp:
        print(
            "[rollback] no checkpoint at .qoder/state/checkpoint.json — "
            "either no run has happened yet or state was cleaned.",
            file=sys.stderr,
        )
        return 2
    sha = cp.get("commit_sha")
    if not sha:
        print("[rollback] checkpoint is missing commit_sha", file=sys.stderr)
        return 2

    if not args.yes:
        print(
            "[rollback] refusing to run without --yes (this is destructive).\n"
            f"[rollback] would restore tree to commit {sha[:12]} "
            f"on branch {cp.get('branch')!r} using scope={args.scope}.",
            file=sys.stderr,
        )
        return 2

    actions: List[str] = []
    if args.scope == "changed":
        files = _changed_files()
        # Don't roll back the integration scripts themselves or the
        # local config — that would erase the workflow.
        protect = {
            "scripts/clean_state.py",
            "scripts/rollback.py",
            "scripts/qoder_invoke.py",
            "scripts/run_self_supervisor_qoder.py",
            "scripts/preflight.py",
            "scripts/verify_delivery.py",
            "scripts/run_tests.py",
            "scripts/bootstrap_mac.sh",
            "supervisor_config.json",
            "README.md",
            ".gitignore",
            ".qoder/commands/self-supervisor.md",
            ".qoder/skills/self-supervisor-v1/SKILL.md",
        }
        files = [f for f in files if f not in protect]
        if not files:
            print("[rollback] no changed files recorded; nothing to do.")
            return 0
        # Process one file at a time — a single bulk
        # ``git checkout <sha> -- <files>`` aborts wholesale when any
        # one path is missing from <sha>, which would leave legitimate
        # modifications un-restored.
        for f in files:
            existed_code, _out, _err = _git(["cat-file", "-e", f"{sha}:{f}"])
            if existed_code == 0:
                code, _out, err = _git(["checkout", sha, "--", f])
                if code == 0:
                    actions.append(f"restored {f} from {sha[:12]}")
                else:
                    actions.append(f"failed to restore {f}: {err.strip()}")
            else:
                target = REPO_ROOT / f
                if target.exists():
                    try:
                        if target.is_dir() and not target.is_symlink():
                            shutil.rmtree(target)
                        else:
                            target.unlink()
                        actions.append(f"deleted post-checkpoint file {f}")
                    except OSError as exc:
                        actions.append(f"could not delete {f}: {exc}")
                else:
                    actions.append(f"already absent: {f}")
    else:
        if cp.get("was_dirty") and not args.allow_dirty_baseline:
            print(
                "[rollback] refusing --scope=hard: the checkpoint recorded "
                "was_dirty=true, so a hard reset would also discard your "
                "pre-existing uncommitted work. Re-run with "
                "--allow-dirty-baseline if that is what you want, or use "
                "--scope=changed instead.",
                file=sys.stderr,
            )
            return 2
        roots = _allowed_roots()
        code, _out, err = _git(["reset", "--hard", sha])
        if code != 0:
            print(f"[rollback] hard reset failed: {err.strip()}", file=sys.stderr)
            return 1
        actions.append(f"reset --hard {sha[:12]}")
        if roots:
            code, _out, err = _git(["clean", "-fd", "--", *roots])
            if code != 0:
                actions.append(f"clean failed: {err.strip()}")
            else:
                actions.append(f"cleaned untracked under {roots}")

    summary = {
        "schema_version": 1,
        "scope": args.scope,
        "checkpoint_commit": sha,
        "actions": actions,
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        for a in actions:
            print(f"[rollback] {a}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
