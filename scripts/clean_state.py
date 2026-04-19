#!/usr/bin/env python3
"""Safely clear stale per-run state from the self-supervisor workspace.

What gets cleaned by default
----------------------------

- everything under ``.qoder/state/`` (recreated with an empty ``tasks/`` dir)
- transient artifact files in ``artifacts/``:
  - ``current_request.md``
  - ``preflight_report.json``
  - ``task-*_scratch.md``

What is preserved
-----------------

- ``artifacts/delivery_report.json`` and ``artifacts/user_acceptance.md``
  — these are the "latest delivery" record and are intentionally
  retained so a previously sealed run can still be inspected after a
  cleanup. Pass ``--all`` to also remove them.

- Anything that is not in the ignore-list above. This script never
  touches user source code, ``tests/``, ``scripts/``, ``src/`` or any
  path outside ``.qoder/state`` and ``artifacts/``.

Flags
-----

    --dry-run   show what would be removed; do not delete anything
    --all       also remove delivery_report.json and user_acceptance.md
    --json      emit a JSON summary on stdout
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List


REPO_ROOT = Path(__file__).resolve().parent.parent
STATE = REPO_ROOT / ".qoder" / "state"
TASKS = STATE / "tasks"
ARTIFACTS = REPO_ROOT / "artifacts"
DELIVERY_REPORT = ARTIFACTS / "delivery_report.json"
USER_ACCEPTANCE = ARTIFACTS / "user_acceptance.md"
TRANSIENT_ARTIFACT_NAMES = ("current_request.md", "preflight_report.json")


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(REPO_ROOT))
    except ValueError:
        return str(p)


def transient_artifact_names() -> List[str]:
    return list(TRANSIENT_ARTIFACT_NAMES)


def _candidates(remove_delivery: bool) -> List[Path]:
    out: List[Path] = []
    if STATE.exists():
        out.extend(sorted(STATE.iterdir()))
    if ARTIFACTS.exists():
        for child in sorted(ARTIFACTS.iterdir()):
            name = child.name
            if name in TRANSIENT_ARTIFACT_NAMES:
                out.append(child)
            elif name.startswith("task-") and name.endswith("_scratch.md"):
                out.append(child)
    if remove_delivery:
        if DELIVERY_REPORT.exists():
            out.append(DELIVERY_REPORT)
        if USER_ACCEPTANCE.exists():
            out.append(USER_ACCEPTANCE)
    return out


def _delete(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Clear stale per-run state from the self-supervisor workspace.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--all",
        action="store_true",
        help="also remove delivery_report.json and user_acceptance.md",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    targets = _candidates(remove_delivery=args.all)
    removed: List[str] = []

    if not args.dry_run:
        for t in targets:
            try:
                _delete(t)
                removed.append(_rel(t))
            except OSError as exc:
                print(f"[clean] failed to remove {_rel(t)}: {exc}", file=sys.stderr)
        # Recreate empty tasks directory so subsequent runs do not have
        # to special-case its absence.
        TASKS.mkdir(parents=True, exist_ok=True)

    summary: Dict[str, Any] = {
        "schema_version": 1,
        "dry_run": args.dry_run,
        "remove_delivery": args.all,
        "candidates": [_rel(p) for p in targets],
        "removed": removed,
        "preserved": (
            []
            if args.all
            else [
                _rel(p)
                for p in (DELIVERY_REPORT, USER_ACCEPTANCE)
                if p.exists()
            ]
        ),
    }

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        verb = "would remove" if args.dry_run else "removed"
        for p in summary["candidates" if args.dry_run else "removed"]:
            print(f"[clean] {verb} {p}")
        if summary["preserved"]:
            for p in summary["preserved"]:
                print(f"[clean] preserved {p} (use --all to remove)")
        if not summary["candidates"]:
            print("[clean] nothing to clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
