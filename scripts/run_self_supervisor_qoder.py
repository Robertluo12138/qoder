#!/usr/bin/env python3
"""Qoder self-supervisor orchestrator.

Given a natural-language request, this script drives the full workflow:

    preflight -> plan -> write -> test -> review -> audit -> seal

and emits ``artifacts/delivery_report.json``. The companion script
``verify_delivery.py`` then produces ``artifacts/user_acceptance.md``.

Design principles
-----------------

* **Deterministic and conservative.** The audit uses concrete checks
  (test exit code, file diff, scope intersection), never model judgment.
* **Observable.** Every stage writes a status, and the final report
  embeds the raw stage outputs.
* **Graceful degradation.** When the ``qoder`` CLI is missing or no
  ``qoder_exec_template`` is configured, the workflow still produces a
  valid, verifiable delivery: task cards are written, tests still run,
  and the report is sealed as ``sealed_advisory`` so verify can surface
  the partial nature of the run.

Entry point
-----------

    python scripts/run_self_supervisor_qoder.py --request "<prompt>"
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
ARTIFACTS = REPO_ROOT / "artifacts"
STATE = REPO_ROOT / ".qoder" / "state"
TASKS_DIR = STATE / "tasks"
DELIVERY_REPORT = ARTIFACTS / "delivery_report.json"
PLAN_PATH = STATE / "plan.json"

# Directories we never descend into when snapshotting the filesystem,
# regardless of user-configured ignore patterns. ``.git`` has its own
# objects that change unpredictably and are not relevant to the diff.
ALWAYS_SKIP_DIRS = {".git"}


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def utc_now() -> str:
    """Return a compact ISO-8601 UTC timestamp."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_config() -> Dict[str, Any]:
    """Load ``supervisor_config.json``. Returns ``{}`` on missing/invalid."""
    cfg_path = REPO_ROOT / "supervisor_config.json"
    if not cfg_path.exists():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(
            f"[orchestrator] WARNING: supervisor_config.json is invalid: {exc}",
            file=sys.stderr,
        )
        return {}


def ensure_dirs() -> None:
    """Create output directories the workflow needs to write into."""
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    TASKS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Stage 1 — PREFLIGHT
# ---------------------------------------------------------------------------

def run_preflight() -> Dict[str, Any]:
    """Shell out to ``scripts/preflight.py`` and parse its JSON output."""
    cmd = [sys.executable, str(SCRIPT_DIR / "preflight.py"), "--json"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return {"ready": False, "error": "preflight timed out after 60s"}
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        report = {
            "ready": False,
            "raw_stdout": proc.stdout,
            "raw_stderr": proc.stderr,
        }
    report["_exit_code"] = proc.returncode
    return report


# ---------------------------------------------------------------------------
# Stage 2 — PLAN
# ---------------------------------------------------------------------------

# Rough textual markers that hint at multiple independent tasks.
MULTI_TASK_SIGNALS = (
    "\nand ",
    " then ",
    " after ",
    "finally",
    "first,",
    "second,",
    "third,",
    "1) ",
    "2) ",
    "3) ",
    "1. ",
    "2. ",
    "3. ",
)

# Numbered / bulleted enumeration at the start of a line.
_ENUM_RE = re.compile(r"(?m)^\s*(?:\d+[\).]|[-*])\s+(.+)$")


def is_multi_task(request: str, threshold_chars: int) -> bool:
    """Short terse requests collapse to a single task; long or enumerated
    ones become multi-task. Explicit enumeration (two or more bullets /
    numbered items) always wins."""
    enum_lines = _ENUM_RE.findall(request)
    if len(enum_lines) >= 2:
        return True
    low = request.lower()
    hits = sum(1 for s in MULTI_TASK_SIGNALS if s in low)
    if len(request) < threshold_chars:
        return hits >= 2
    return hits >= 1


def _split_into_segments(request: str) -> List[str]:
    """Split a request into task segments. Prefers newline-based enumeration,
    then inline numbered/bulleted lists, and finally falls back to sentence
    boundaries."""
    numbered = _ENUM_RE.findall(request)
    if len(numbered) >= 2:
        return [s.strip() for s in numbered]
    # Inline enumeration like "1. foo. 2. bar. 3. baz." on a single line.
    inline_parts = re.split(r"(?=(?:^|\s)\d+[\).]\s)", request.strip())
    inline_parts = [
        re.sub(r"^\s*\d+[\).]\s*", "", p).strip().rstrip(".") + "."
        for p in inline_parts if re.sub(r"^\s*\d+[\).]\s*", "", p).strip()
    ]
    # Keep only segments that have real content (more than a trailing period).
    inline_parts = [p for p in inline_parts if len(p) > 1]
    if len(inline_parts) >= 2:
        return inline_parts
    sentences = re.split(r"(?<=[.!?])\s+", request.strip())
    sentences = [s for s in sentences if s.strip()]
    if len(sentences) >= 2:
        return sentences
    return [request.strip()]


def build_plan(request: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Produce a deterministic plan. No model calls; pure heuristics."""
    threshold = int(config.get("single_task_threshold_chars", 200))
    multi = is_multi_task(request, threshold)
    mode = "multi_task" if multi else "single_task"

    if not multi:
        tasks = [
            {
                "id": "task-1",
                "title": (request.strip().split("\n", 1)[0][:80] or "Single task"),
                "description": request.strip(),
                "acceptance": [
                    "Implementation satisfies the user's request.",
                    "`python scripts/run_tests.py` passes (ok or ok_no_tests).",
                    "No files are changed outside `allowed_write_roots`.",
                ],
            }
        ]
    else:
        segments = _split_into_segments(request)
        tasks = [
            {
                "id": f"task-{i + 1}",
                "title": (seg.strip().split("\n", 1)[0][:80] or f"Task {i + 1}"),
                "description": seg.strip(),
                "acceptance": [
                    "Segment-specific implementation is complete.",
                    "Tests still pass after this segment.",
                ],
            }
            for i, seg in enumerate(segments)
        ]

    return {
        "mode": mode,
        "generated_at": utc_now(),
        "request": request,
        "tasks": tasks,
    }


# ---------------------------------------------------------------------------
# Stage 3 — WRITE (scope + snapshot + implementation)
# ---------------------------------------------------------------------------

def compute_write_scope(config: Dict[str, Any]) -> List[str]:
    """Resolve the allowed write-scope roots, de-duplicated in order."""
    roots = config.get("allowed_write_roots") or [
        "scripts", "src", "tests", "artifacts", ".qoder",
    ]
    seen: set = set()
    out: List[str] = []
    for r in roots:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _is_ignored(path: str, patterns: List[str]) -> bool:
    """Return True if ``path`` (repo-relative, forward-slash) is ignored.

    Supports the same pattern syntax as ``preflight.py``: plain fnmatch
    globs (``*.pyc``, ``__pycache__``) and multi-segment path prefixes
    (``.qoder/state``) that apply to everything beneath them.
    """
    if not patterns:
        return False
    parts = path.split("/")
    for pat in patterns:
        if fnmatch.fnmatch(path, pat):
            return True
        for p in parts:
            if fnmatch.fnmatch(p, pat):
                return True
        if "/" in pat and path.startswith(pat.rstrip("/") + "/"):
            return True
    return False


def snapshot_repo(root: Path, ignore: List[str]) -> Dict[str, str]:
    """Compute ``{relative_path: sha1}`` for every file under ``root``
    that is not ignored. Used to detect what the write stage changed."""
    result: Dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = str(Path(dirpath).relative_to(root)).replace(os.sep, "/")
        if rel_dir == ".":
            rel_dir = ""
        # Prune ignored / always-skipped directories in-place so we don't
        # descend into them.
        kept: List[str] = []
        for d in dirnames:
            if d in ALWAYS_SKIP_DIRS:
                continue
            combined = (rel_dir + "/" + d).lstrip("/")
            if _is_ignored(d, ignore) or _is_ignored(combined, ignore):
                continue
            kept.append(d)
        dirnames[:] = kept
        for fn in filenames:
            combined = (rel_dir + "/" + fn).lstrip("/")
            if _is_ignored(fn, ignore) or _is_ignored(combined, ignore):
                continue
            full = Path(dirpath) / fn
            try:
                with full.open("rb") as fh:
                    result[combined] = hashlib.sha1(fh.read()).hexdigest()
            except OSError:
                # Unreadable files are treated as absent; skip silently.
                continue
    return result


def detect_qoder(cli: str) -> Dict[str, Any]:
    """Discover whether the qoder CLI is available and responsive."""
    path = shutil.which(cli)
    if not path:
        return {"available": False, "reason": "not_on_path", "cli": cli}
    for probe in (["--version"], ["-V"], ["--help"]):
        try:
            proc = subprocess.run(
                [cli, *probe], capture_output=True, text=True, timeout=10
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {
                "available": False,
                "reason": f"probe_failed: {exc}",
                "cli": cli,
                "path": path,
            }
        if proc.returncode == 0:
            head_text = (proc.stdout or proc.stderr).strip().splitlines()
            return {
                "available": True,
                "cli": cli,
                "path": path,
                "probe": probe[0],
                "version_head": head_text[0][:200] if head_text else "",
            }
    # Executable present but doesn't respond to any common probe — still
    # callable, just undiscoverable, so we mark it available but unknown.
    return {"available": True, "cli": cli, "path": path, "version_head": "unknown (probes failed)"}


def write_task_card(task: Dict[str, Any], plan_mode: str) -> Path:
    """Write a human-readable markdown card describing a task."""
    card = TASKS_DIR / f"{task['id']}.md"
    lines = [
        f"# {task['title']}",
        "",
        f"- plan_mode: {plan_mode}",
        f"- task_id: {task['id']}",
        f"- generated_at: {utc_now()}",
        "",
        "## Description",
        "",
        task["description"],
        "",
        "## Acceptance criteria",
        "",
    ]
    for crit in task.get("acceptance", []):
        lines.append(f"- {crit}")
    lines.append("")
    card.write_text("\n".join(lines), encoding="utf-8")
    return card


def invoke_qoder(task: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """Invoke the real qoder CLI for a task.

    The concrete argv is driven by ``qoder_exec_template`` in the config,
    which is an argv list with placeholders: ``{prompt}``, ``{title}``,
    ``{task_id}``, ``{card_path}``. If no template is configured we
    intentionally do *not* guess the CLI's interface; we return an
    advisory result instead.
    """
    cli = config.get("qoder_cli", "qoder")
    template = config.get("qoder_exec_template")
    if not template or not isinstance(template, list):
        return {
            "mode": "advisory",
            "note": "no qoder_exec_template configured; qoder was not auto-invoked",
            "card": str((TASKS_DIR / f"{task['id']}.md").relative_to(REPO_ROOT)),
        }
    cmd: List[str] = [cli]
    for arg in template:
        if arg == "{prompt}":
            cmd.append(task["description"])
        elif arg == "{title}":
            cmd.append(task["title"])
        elif arg == "{task_id}":
            cmd.append(task["id"])
        elif arg == "{card_path}":
            cmd.append(str((TASKS_DIR / f"{task['id']}.md").relative_to(REPO_ROOT)))
        else:
            cmd.append(str(arg))
    try:
        proc = subprocess.run(
            cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=1800
        )
        return {
            "mode": "qoder_cli",
            "command": cmd,
            "exit_code": proc.returncode,
            "stdout_tail": (proc.stdout or "")[-2000:],
            "stderr_tail": (proc.stderr or "")[-2000:],
        }
    except subprocess.TimeoutExpired:
        return {
            "mode": "qoder_cli",
            "command": cmd,
            "exit_code": 124,
            "note": "qoder invocation timed out after 1800s",
        }
    except OSError as exc:
        return {
            "mode": "qoder_cli",
            "command": cmd,
            "exit_code": 127,
            "note": f"qoder invocation failed: {exc}",
        }


def fallback_write(task: Dict[str, Any], allowed: List[str]) -> Dict[str, Any]:
    """Advisory fallback used when qoder can't be invoked safely.

    Rather than fabricate code, record the pending task under
    ``artifacts/`` so the user has a clear artifact of what needs doing.
    """
    scratch = ARTIFACTS / f"{task['id']}_scratch.md"
    lines = [
        f"# Pending implementation: {task['title']}",
        "",
        "The self-supervisor produced this task card because either the",
        "qoder CLI was unavailable or no `qoder_exec_template` is",
        "configured in `supervisor_config.json`. No source code was",
        "modified automatically.",
        "",
        "## Task description",
        "",
        task["description"],
        "",
        "## How to complete",
        "",
        "1. Implement the change in your editor or via qoder.",
        "2. Restrict edits to these allowed roots:",
    ]
    for r in allowed:
        lines.append(f"   - `{r}`")
    lines.append("")
    lines.append("3. Re-run `python scripts/run_self_supervisor_qoder.py --request ...`")
    lines.append("   (or `python scripts/verify_delivery.py` after manual edits).")
    lines.append("")
    scratch.write_text("\n".join(lines), encoding="utf-8")
    return {
        "mode": "fallback_advisory",
        "artifact": str(scratch.relative_to(REPO_ROOT)),
    }


def perform_write(
    plan: Dict[str, Any],
    config: Dict[str, Any],
    qoder_info: Dict[str, Any],
    dry_run: bool,
) -> Dict[str, Any]:
    """Run the write stage for every task in the plan."""
    allowed = compute_write_scope(config)
    use_qoder = bool(qoder_info.get("available") and config.get("qoder_exec_template"))
    invocations: List[Dict[str, Any]] = []
    cards: List[str] = []
    for task in plan["tasks"]:
        card = write_task_card(task, plan["mode"])
        cards.append(str(card.relative_to(REPO_ROOT)))
        if dry_run:
            invocations.append({"task_id": task["id"], "mode": "dry_run"})
        elif use_qoder:
            invocations.append({"task_id": task["id"], **invoke_qoder(task, config)})
        else:
            invocations.append({"task_id": task["id"], **fallback_write(task, allowed)})
    if dry_run:
        impl_mode = "dry_run"
    elif use_qoder:
        impl_mode = "qoder_cli"
    else:
        impl_mode = "fallback_advisory"
    return {
        "implementation_mode": impl_mode,
        "allowed_write_roots": allowed,
        "task_cards": cards,
        "invocations": invocations,
    }


# ---------------------------------------------------------------------------
# Stage 4 — TEST
# ---------------------------------------------------------------------------

def run_unified_tests() -> Dict[str, Any]:
    """Call ``scripts/run_tests.py`` — the single source of test truth."""
    cmd = [sys.executable, str(SCRIPT_DIR / "run_tests.py")]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return {"passed": False, "status": "timeout", "exit_code": 124}
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {
            "passed": False,
            "status": "error",
            "raw_stdout": proc.stdout,
            "raw_stderr": proc.stderr,
        }
    payload["_runner_exit_code"] = proc.returncode
    return payload


# ---------------------------------------------------------------------------
# Stage 5 — REVIEW
# ---------------------------------------------------------------------------

def _inside_any_root(path: str, roots: List[str]) -> bool:
    for r in roots:
        r_norm = r.rstrip("/")
        if path == r_norm or path.startswith(r_norm + "/"):
            return True
    return False


def review_changes(
    before: Dict[str, str], after: Dict[str, str], allowed: List[str]
) -> Dict[str, Any]:
    """Classify changes between two snapshots and check scope."""
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    modified = sorted(p for p in (set(before) & set(after)) if before[p] != after[p])
    changed = sorted({*added, *removed, *modified})
    out_of_scope = [p for p in changed if not _inside_any_root(p, allowed)]
    notes: List[str] = []
    if not changed:
        notes.append("No in-scope file changes detected (advisory-mode run is expected to look like this).")
    if len(changed) > 25:
        notes.append("Large changeset — consider splitting into smaller tasks.")
    if removed:
        notes.append(f"{len(removed)} file(s) deleted; confirm the removals are intentional.")
    return {
        "added": added,
        "removed": removed,
        "modified": modified,
        "changed": changed,
        "out_of_scope": out_of_scope,
        "scope_respected": not out_of_scope,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Stage 6 — AUDIT (deterministic gates)
# ---------------------------------------------------------------------------

def audit(
    tests: Dict[str, Any],
    review: Dict[str, Any],
    plan: Dict[str, Any],
) -> Dict[str, Any]:
    """Apply the hard gates. A delivery is accepted only when all pass."""
    checks = [
        {
            "name": "tests_passed",
            "passed": bool(tests.get("passed")),
            "detail": tests.get("status"),
        },
        {
            "name": "scope_respected",
            "passed": bool(review.get("scope_respected")),
            "detail": {"out_of_scope_count": len(review.get("out_of_scope", []))},
        },
        {
            "name": "plan_has_tasks",
            "passed": bool(plan.get("tasks")),
            "detail": {"task_count": len(plan.get("tasks", []))},
        },
    ]
    return {"checks": checks, "all_passed": all(c["passed"] for c in checks)}


# ---------------------------------------------------------------------------
# Stage 7 — SEAL
# ---------------------------------------------------------------------------

def seal_delivery(payload: Dict[str, Any]) -> Path:
    """Write the final delivery report atomically enough for our needs."""
    DELIVERY_REPORT.write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    return DELIVERY_REPORT


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def _assemble(
    *,
    request: str,
    config: Dict[str, Any],
    plan: Optional[Dict[str, Any]],
    write: Optional[Dict[str, Any]],
    tests: Optional[Dict[str, Any]],
    review: Optional[Dict[str, Any]],
    audit_report: Optional[Dict[str, Any]],
    stage_status: Dict[str, str],
    delivery_status: str,
    preflight: Dict[str, Any],
    qoder_info: Optional[Dict[str, Any]],
    extras: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "repo_root": str(REPO_ROOT),
        "user_request": request,
        "config_used": {
            "project_type": config.get("project_type"),
            "test_command": config.get("test_command"),
            "allowed_write_roots": config.get("allowed_write_roots"),
            "allow_dirty_repo": config.get("allow_dirty_repo"),
            "qoder_cli": config.get("qoder_cli"),
            "qoder_exec_template": config.get("qoder_exec_template"),
        },
        "stages": {
            "preflight": preflight,
            "plan": plan,
            "write": write,
            "tests": tests,
            "review": review,
            "audit": audit_report,
        },
        "stage_status": stage_status,
        "qoder_info": qoder_info,
        "delivery_status": delivery_status,
        "next_step": "python scripts/verify_delivery.py",
        **extras,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Run the Qoder self-supervisor orchestrator.",
    )
    parser.add_argument("--request", help="natural-language task request")
    parser.add_argument(
        "--request-file", help="read the request from a file instead of --request"
    )
    parser.add_argument(
        "--force", action="store_true", help="proceed past non-ok preflight"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="plan + write task cards, but skip any qoder or fallback writes",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="also emit the final report JSON to stdout",
    )
    args = parser.parse_args(argv)

    if not args.request and not args.request_file:
        print("error: provide --request or --request-file", file=sys.stderr)
        return 2

    # Resolve request text
    if args.request_file:
        try:
            request = Path(args.request_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"error: cannot read --request-file: {exc}", file=sys.stderr)
            return 2
    else:
        request = args.request.strip()

    if not request:
        print("error: request is empty", file=sys.stderr)
        return 2

    config = load_config()
    ensure_dirs()

    stage_status: Dict[str, str] = {}
    started = time.monotonic()

    # 1. Preflight ---------------------------------------------------------
    preflight = run_preflight()
    stage_status["preflight"] = (
        "ok" if preflight.get("ready") else ("warn" if args.force else "blocked")
    )
    if not preflight.get("ready") and not args.force:
        payload = _assemble(
            request=request,
            config=config,
            plan=None,
            write=None,
            tests=None,
            review=None,
            audit_report=None,
            stage_status=stage_status,
            delivery_status="blocked_preflight",
            preflight=preflight,
            qoder_info=None,
            extras={
                "hint": (
                    "run `python scripts/preflight.py --fix` or re-run with --force"
                ),
            },
        )
        seal_delivery(payload)
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"[orchestrator] blocked at preflight. Report: {DELIVERY_REPORT}")
        return 1

    # 2. Plan --------------------------------------------------------------
    plan = build_plan(request, config)
    PLAN_PATH.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    stage_status["plan"] = "ok"

    # 3. Write (snapshot → implement → snapshot again) --------------------
    ignore = list(config.get("ignore_paths") or [])
    snapshot_before = snapshot_repo(REPO_ROOT, ignore)
    qoder_info = detect_qoder(config.get("qoder_cli", "qoder"))
    write = perform_write(plan, config, qoder_info, args.dry_run)
    stage_status["write"] = "ok"
    snapshot_after = snapshot_repo(REPO_ROOT, ignore)

    # 4. Test --------------------------------------------------------------
    tests = run_unified_tests()
    stage_status["test"] = "ok" if tests.get("passed") else "failed"

    # 5. Review ------------------------------------------------------------
    allowed = compute_write_scope(config)
    review = review_changes(snapshot_before, snapshot_after, allowed)
    stage_status["review"] = "ok" if review["scope_respected"] else "warn"

    # 6. Audit -------------------------------------------------------------
    audit_report = audit(tests, review, plan)
    stage_status["audit"] = "ok" if audit_report["all_passed"] else "blocked"

    # 7. Seal --------------------------------------------------------------
    if audit_report["all_passed"]:
        advisory = write.get("implementation_mode") in ("fallback_advisory", "dry_run")
        delivery_status = "sealed_advisory" if advisory else "sealed"
    else:
        delivery_status = "blocked_audit"

    total_s = round(time.monotonic() - started, 3)

    payload = _assemble(
        request=request,
        config=config,
        plan=plan,
        write=write,
        tests=tests,
        review=review,
        audit_report=audit_report,
        stage_status=stage_status,
        delivery_status=delivery_status,
        preflight=preflight,
        qoder_info=qoder_info,
        extras={"total_duration_s": total_s},
    )
    seal_delivery(payload)

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"[orchestrator] delivery_status={delivery_status}")
        print(f"[orchestrator] implementation_mode={write.get('implementation_mode')}")
        print(f"[orchestrator] report: {DELIVERY_REPORT}")
        print(f"[orchestrator] next: python scripts/verify_delivery.py")

    return 0 if delivery_status in ("sealed", "sealed_advisory") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
