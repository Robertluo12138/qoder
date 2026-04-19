#!/usr/bin/env python3
"""Qoder-native self-supervisor orchestrator."""

from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from qoder_invoke import invoke_qoder_json  # noqa: E402


ARTIFACTS = REPO_ROOT / "artifacts"
STATE = REPO_ROOT / ".qoder" / "state"
TASKS_DIR = STATE / "tasks"
PLAN_PATH = STATE / "plan.json"
CHECKPOINT_PATH = STATE / "checkpoint.json"
DELIVERY_REPORT = ARTIFACTS / "delivery_report.json"
ALWAYS_SKIP_DIRS = {".git"}
WRITE_PERMISSION_HINTS = (
    "permission",
    "approve",
    "approval",
    "confirm",
    "confirmation",
    "interactive",
    "non-interactive",
    "non interactive",
    "yolo",
    "dangerously-skip-permissions",
)
DEFAULT_BROAD_TASK_MARKERS = (
    "entire repo",
    "whole repo",
    "across the codebase",
    "across the repository",
    "large refactor",
    "full migration",
    "rewrite the project",
    "rewrite the codebase",
    "all files",
)


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_config() -> Dict[str, Any]:
    cfg_path = REPO_ROOT / "supervisor_config.json"
    if not cfg_path.exists():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def ensure_dirs() -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    TASKS_DIR.mkdir(parents=True, exist_ok=True)


def run_preflight() -> Dict[str, Any]:
    cmd = [sys.executable, str(SCRIPT_DIR / "preflight.py"), "--json"]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except subprocess.TimeoutExpired:
        return {"ready": False, "error": "preflight timed out after 1800s"}
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "ready": False,
            "error": "invalid_preflight_json",
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    payload["_exit_code"] = proc.returncode
    return payload


def run_unified_tests() -> Dict[str, Any]:
    cmd = [sys.executable, str(SCRIPT_DIR / "run_tests.py")]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except subprocess.TimeoutExpired:
        return {"passed": False, "status": "timeout", "exit_code": 124}
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "passed": False,
            "status": "invalid_json",
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    payload["_runner_exit_code"] = proc.returncode
    return payload


def compute_write_scope(config: Dict[str, Any]) -> List[str]:
    roots = config.get("allowed_write_roots") or ["scripts", "src", "tests", ".qoder", "docs"]
    seen: set[str] = set()
    ordered: List[str] = []
    for root in roots:
        if root not in seen:
            ordered.append(root)
            seen.add(root)
    return ordered


def write_stage_tool_policy(config: Dict[str, Any]) -> Dict[str, Any]:
    allowed_tools = config.get("qoder_write_allowed_tools")
    if allowed_tools is None:
        allowed_tools = ["Bash", "Edit"]
    disallowed_tools = config.get("qoder_write_disallowed_tools") or []
    return {
        "allowed_tools": [str(item) for item in allowed_tools],
        "disallowed_tools": [str(item) for item in disallowed_tools],
        "try_without_yolo_first": bool(config.get("qoder_write_try_without_yolo_first", True)),
        "no_yolo_timeout_seconds": int(config.get("qoder_write_no_yolo_timeout_seconds", 60)),
        "yolo_enabled": bool(config.get("qoder_write_yolo", True)),
        "yolo_fallback_on_permission_error": bool(
            config.get("qoder_write_yolo_fallback_on_permission_error", True)
        ),
    }


def needs_yolo_retry(result: Dict[str, Any]) -> bool:
    if result.get("ok"):
        return False
    chunks: List[str] = []
    for key in ("error", "stderr", "stdout", "text"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            chunks.append(value)
    events = result.get("events")
    if events:
        try:
            chunks.append(json.dumps(events, ensure_ascii=False))
        except TypeError:
            chunks.append(str(events))
    haystack = "\n".join(chunks).lower()
    return any(hint in haystack for hint in WRITE_PERMISSION_HINTS)


def run_git(args: List[str]) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", "git not on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", "git timed out after 60s"


def git_branch_name() -> str:
    code, out, _err = run_git(["rev-parse", "--abbrev-ref", "HEAD"])
    return out.strip() if code == 0 and out.strip() else "unknown"


def git_head_sha() -> str | None:
    code, out, _err = run_git(["rev-parse", "HEAD"])
    if code == 0 and out.strip():
        return out.strip()
    return None


def git_path_info() -> Dict[str, Any]:
    branch = git_branch_name()
    commit_sha = git_head_sha()
    code_git_dir, out_git_dir, _ = run_git(["rev-parse", "--git-dir"])
    code_common_dir, out_common_dir, _ = run_git(["rev-parse", "--git-common-dir"])
    code_top, out_top, _ = run_git(["rev-parse", "--show-toplevel"])

    git_dir = out_git_dir.strip() if code_git_dir == 0 else None
    common_dir = out_common_dir.strip() if code_common_dir == 0 else None
    top_level = out_top.strip() if code_top == 0 else None
    return {
        "branch": branch,
        "commit_sha": commit_sha,
        "repo_root": str(REPO_ROOT),
        "git_dir": git_dir,
        "git_common_dir": common_dir,
        "git_top_level": top_level,
        "is_worktree": bool(git_dir and common_dir and git_dir != common_dir),
    }


def assess_auto_write_guardrail(
    request: str,
    plan: Dict[str, Any] | None,
    review: Dict[str, Any] | None,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    request_chars = len(request)
    planned_tasks = len((plan or {}).get("tasks") or [])
    changed_files = len((review or {}).get("changed") or [])

    max_request_chars = int(config.get("guardrail_max_request_chars", 1200))
    max_planned_tasks = int(config.get("guardrail_max_planned_tasks", 3))
    max_changed_files = int(config.get("guardrail_max_changed_files", 8))
    markers = [str(item).lower() for item in (config.get("guardrail_broad_request_markers") or DEFAULT_BROAD_TASK_MARKERS)]

    request_lower = request.lower()
    broad_hits = [marker for marker in markers if marker in request_lower]
    reasons: List[str] = []

    if request_chars > max_request_chars:
        reasons.append(
            f"request length {request_chars} exceeds recommended unattended limit {max_request_chars}"
        )
    if planned_tasks > max_planned_tasks:
        reasons.append(
            f"planned task count {planned_tasks} exceeds recommended unattended limit {max_planned_tasks}"
        )
    if review is not None and changed_files > max_changed_files:
        reasons.append(
            f"changed file count {changed_files} exceeds recommended unattended limit {max_changed_files}"
        )
    if broad_hits:
        reasons.append(
            "request appears broad for unattended auto-write: " + ", ".join(broad_hits)
        )

    status = (
        "not_recommended_for_unattended_auto_write"
        if reasons
        else "recommended_for_unattended_auto_write"
    )
    return {
        "status": status,
        "request_chars": request_chars,
        "planned_tasks": planned_tasks,
        "changed_files": changed_files,
        "broad_markers_found": broad_hits,
        "reasons": reasons,
        "thresholds": {
            "max_request_chars": max_request_chars,
            "max_planned_tasks": max_planned_tasks,
            "max_changed_files": max_changed_files,
        },
    }


def _is_ignored(path: str, patterns: List[str]) -> bool:
    if not patterns:
        return False
    parts = path.split("/")
    for pat in patterns:
        if fnmatch.fnmatch(path, pat):
            return True
        for part in parts:
            if fnmatch.fnmatch(part, pat):
                return True
        if "/" in pat and path.startswith(pat.rstrip("/") + "/"):
            return True
    return False


def git_status_entries(ignore: List[str]) -> List[Dict[str, str]]:
    code, out, _err = run_git(["status", "--porcelain"])
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
        if _is_ignored(path, ignore):
            continue
        dirty.append({"status": status, "path": path})
    return dirty


def snapshot_repo(root: Path, ignore: List[str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = str(Path(dirpath).relative_to(root)).replace(os.sep, "/")
        if rel_dir == ".":
            rel_dir = ""
        kept: List[str] = []
        for dirname in dirnames:
            if dirname in ALWAYS_SKIP_DIRS:
                continue
            combined = (rel_dir + "/" + dirname).lstrip("/")
            if _is_ignored(dirname, ignore) or _is_ignored(combined, ignore):
                continue
            kept.append(dirname)
        dirnames[:] = kept
        for filename in filenames:
            combined = (rel_dir + "/" + filename).lstrip("/")
            if _is_ignored(filename, ignore) or _is_ignored(combined, ignore):
                continue
            full = Path(dirpath) / filename
            try:
                with full.open("rb") as handle:
                    result[combined] = hashlib.sha1(handle.read()).hexdigest()
            except OSError:
                continue
    return result


def capture_checkpoint(allowed: List[str], ignore: List[str]) -> Dict[str, Any]:
    dirty = git_status_entries(ignore)
    checkpoint = {
        "schema_version": 2,
        "created_at": utc_now(),
        "branch": git_branch_name(),
        "commit_sha": git_head_sha(),
        "was_dirty": bool(dirty),
        "dirty_entries": dirty,
        "allowed_write_roots": allowed,
        "ignore_paths": ignore,
        "review_source": "git_status" if not dirty else "filesystem_snapshot",
    }
    CHECKPOINT_PATH.write_text(json.dumps(checkpoint, indent=2) + "\n", encoding="utf-8")
    return checkpoint


def _inside_any_root(path: str, roots: List[str]) -> bool:
    for root in roots:
        root = root.rstrip("/")
        if path == root or path.startswith(root + "/"):
            return True
    return False


def review_changes_from_snapshots(
    before: Dict[str, str],
    after: Dict[str, str],
    allowed: List[str],
) -> Dict[str, Any]:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    modified = sorted(path for path in (set(before) & set(after)) if before[path] != after[path])
    changed = sorted({*added, *removed, *modified})
    out_of_scope = [path for path in changed if not _inside_any_root(path, allowed)]
    return {
        "diff_source": "filesystem_snapshot",
        "added": added,
        "removed": removed,
        "modified": modified,
        "changed": changed,
        "out_of_scope": out_of_scope,
        "scope_respected": not out_of_scope,
    }


def review_changes_from_git_status(ignore: List[str], allowed: List[str]) -> Dict[str, Any]:
    entries = git_status_entries(ignore)
    added: List[str] = []
    removed: List[str] = []
    modified: List[str] = []
    for entry in entries:
        status = entry["status"]
        path = entry["path"]
        if status == "??":
            added.append(path)
        elif "D" in status:
            removed.append(path)
        else:
            modified.append(path)
    changed = sorted({*added, *removed, *modified})
    out_of_scope = [path for path in changed if not _inside_any_root(path, allowed)]
    return {
        "diff_source": "git_status",
        "git_status_entries": entries,
        "added": sorted(added),
        "removed": sorted(removed),
        "modified": sorted(modified),
        "changed": changed,
        "out_of_scope": out_of_scope,
        "scope_respected": not out_of_scope,
    }


def review_changes(
    *,
    before: Dict[str, str],
    after: Dict[str, str],
    checkpoint: Dict[str, Any],
    ignore: List[str],
    allowed: List[str],
) -> Dict[str, Any]:
    if checkpoint.get("review_source") == "git_status":
        return review_changes_from_git_status(ignore, allowed)
    report = review_changes_from_snapshots(before, after, allowed)
    report["baseline_reason"] = "pre-write working tree was already dirty"
    return report


def git_diff_text(paths: List[str]) -> str:
    if not paths:
        return "(no changed files)"
    code, out, err = run_git(["diff", "--", *paths])
    if code == 0 and out.strip():
        return out
    if code == 0:
        return "(git diff empty for changed files)"
    return f"(git diff unavailable: {err.strip()})"


def write_task_card(task: Dict[str, Any], plan_mode: str) -> str:
    path = TASKS_DIR / f"{task['id']}.md"
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
        "## Acceptance Criteria",
        "",
    ]
    for item in task.get("acceptance", []):
        lines.append(f"- {item}")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path.relative_to(REPO_ROOT))


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
ENUM_RE = re.compile(r"(?m)^\s*(?:\d+[\).]|[-*])\s+(.+)$")


def should_default_single_task(request: str, threshold_chars: int) -> bool:
    if len(ENUM_RE.findall(request)) >= 2:
        return False
    low = request.lower()
    hits = sum(1 for marker in MULTI_TASK_SIGNALS if marker in low)
    if len(request) < threshold_chars:
        return hits < 2
    return hits == 0


def normalize_plan(request: str, config: Dict[str, Any], parsed: Dict[str, Any]) -> Dict[str, Any]:
    tasks = list(parsed.get("tasks") or [])
    mode = str(parsed.get("mode") or ("multi_task" if len(tasks) > 1 else "single_task"))
    threshold = int(config.get("single_task_threshold_chars", 200))

    normalized_tasks: List[Dict[str, Any]] = []
    for idx, task in enumerate(tasks, start=1):
        normalized_tasks.append(
            {
                "id": str(task.get("id") or f"task-{idx}"),
                "title": str(task.get("title") or f"Task {idx}")[:80],
                "description": str(task.get("description") or "").strip(),
                "acceptance": [str(item) for item in (task.get("acceptance") or [])],
            }
        )

    if not normalized_tasks:
        normalized_tasks = [
            {
                "id": "task-1",
                "title": request.strip().split("\n", 1)[0][:80] or "Task 1",
                "description": request.strip(),
                "acceptance": [
                    "Implementation satisfies the user's request.",
                    "`python scripts/run_tests.py` passes.",
                ],
            }
        ]
        mode = "single_task"

    if should_default_single_task(request, threshold) and len(normalized_tasks) > 1:
        merged_acceptance: List[str] = []
        for task in normalized_tasks:
            for item in task.get("acceptance", []):
                if item not in merged_acceptance:
                    merged_acceptance.append(item)
        normalized_tasks = [
            {
                "id": "task-1",
                "title": normalized_tasks[0]["title"],
                "description": request.strip(),
                "acceptance": merged_acceptance or [
                    "Implementation satisfies the user's request.",
                    "`python scripts/run_tests.py` passes.",
                ],
            }
        ]
        mode = "single_task"

    return {
        "mode": mode if len(normalized_tasks) > 1 else "single_task",
        "generated_at": utc_now(),
        "request": request,
        "tasks": normalized_tasks,
    }


def build_plan_prompt(request: str, config: Dict[str, Any]) -> str:
    threshold = int(config.get("single_task_threshold_chars", 200))
    return f"""
You are the planner for a Qoder-native self-supervisor workflow.
Read the repository context as needed, but do not modify files.

Return only valid JSON with this exact shape:
{{
  "mode": "single_task" | "multi_task",
  "tasks": [
    {{
      "id": "task-1",
      "title": "short title",
      "description": "what to do",
      "acceptance": ["criterion 1", "criterion 2"]
    }}
  ]
}}

Rules:
- Default to exactly one task for small requests.
- Only split into multiple tasks when the request clearly contains
  independent sub-goals.
- Keep titles short and concrete.
- Acceptance criteria must mention the unified test entry
  `python scripts/run_tests.py`.
- Do not mention Codex or any non-Qoder executor.
- Do not include Markdown fences or explanatory prose.

Small-task threshold: {threshold} characters.

User request:
{request}
""".strip()


def build_write_prompt(task: Dict[str, Any], allowed: List[str], tool_policy: Dict[str, Any]) -> str:
    return f"""
You are the writer stage of a Qoder-native self-supervisor workflow.
Make the requested code changes now.

Hard constraints:
- Only modify files inside these allowed roots: {json.dumps(allowed)}
- Do not touch files outside that scope.
- Your Qoder CLI tool policy allows these tools: {json.dumps(tool_policy.get("allowed_tools", []))}
- Your Qoder CLI tool policy disallows these tools: {json.dumps(tool_policy.get("disallowed_tools", []))}
- Use Bash only for local inspection or validation related to this task.
- Use Edit only for in-scope file changes that are necessary for the task.
- Keep the change as small as possible.
- After finishing, return only JSON with this shape:
  {{
    "summary": "what changed",
    "claimed_completed": true,
    "touched_files": ["path/one", "path/two"]
  }}
- Do not wrap the JSON in Markdown fences.

Current task:
{json.dumps(task, ensure_ascii=False, indent=2)}
""".strip()


def build_review_prompt(
    request: str,
    plan: Dict[str, Any],
    review: Dict[str, Any],
    tests: Dict[str, Any],
    diff_text: str,
) -> str:
    payload = {
        "request": request,
        "plan_mode": plan.get("mode"),
        "tasks": plan.get("tasks"),
        "review_scope": {
            "changed": review.get("changed"),
            "out_of_scope": review.get("out_of_scope"),
            "scope_respected": review.get("scope_respected"),
        },
        "tests": {
            "status": tests.get("status"),
            "passed": tests.get("passed"),
            "exit_code": tests.get("exit_code"),
        },
    }
    return f"""
You are the reviewer stage of a Qoder-native self-supervisor workflow.
Review the current result without modifying files.

Return only valid JSON with this shape:
{{
  "decision": "approve" | "reject",
  "summary": "short review summary",
  "blocking_issues": ["issue 1"],
  "non_blocking_suggestions": ["suggestion 1"]
}}

Reject if:
- tests failed,
- files changed outside scope,
- the request is clearly incomplete.

Evidence:
{json.dumps(payload, ensure_ascii=False, indent=2)}

Git diff for changed files:
{diff_text}
""".strip()


def run_plan_stage(request: str, config: Dict[str, Any]) -> Dict[str, Any]:
    result = invoke_qoder_json(
        prompt=build_plan_prompt(request, config),
        workspace=REPO_ROOT,
        disallowed_tools=["Edit"],
        max_turns=int(config.get("qoder_plan_max_turns", 12)),
        timeout=int(config.get("qoder_timeout_seconds", 1800)),
    )
    if not result.get("ok"):
        return {"ok": False, "invocation": result}
    parsed = result.get("parsed")
    if not isinstance(parsed, dict):
        return {"ok": False, "invocation": result, "error": "planner_did_not_return_object"}
    plan = normalize_plan(request, config, parsed)
    PLAN_PATH.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "plan": plan, "invocation": result}


def run_write_stage(plan: Dict[str, Any], config: Dict[str, Any], allowed: List[str]) -> Dict[str, Any]:
    tool_policy = write_stage_tool_policy(config)
    invocations: List[Dict[str, Any]] = []
    task_cards: List[str] = []
    for task in plan.get("tasks", []):
        task_cards.append(write_task_card(task, plan.get("mode", "single_task")))
        attempts: List[Dict[str, Any]] = []

        def run_attempt(*, yolo: bool, attempt_name: str) -> Dict[str, Any]:
            timeout = (
                int(tool_policy.get("no_yolo_timeout_seconds", 60))
                if not yolo and tool_policy.get("try_without_yolo_first")
                else int(config.get("qoder_timeout_seconds", 1800))
            )
            result = invoke_qoder_json(
                prompt=build_write_prompt(task, allowed, tool_policy),
                workspace=REPO_ROOT,
                yolo=yolo,
                allowed_tools=tool_policy.get("allowed_tools") or None,
                disallowed_tools=tool_policy.get("disallowed_tools") or None,
                max_turns=int(config.get("qoder_write_max_turns", 40)),
                timeout=timeout,
            )
            attempt_record: Dict[str, Any] = {
                "attempt": attempt_name,
                "yolo": yolo,
                "timeout_seconds": timeout,
                "allowed_tools": tool_policy.get("allowed_tools", []),
                "disallowed_tools": tool_policy.get("disallowed_tools", []),
                "ok": bool(result.get("ok")),
                "command": result.get("command"),
                "exit_code": result.get("exit_code"),
                "session_id": result.get("session_id"),
                "text": result.get("text"),
                "stderr": result.get("stderr"),
            }
            if result.get("ok"):
                attempt_record["writer_result"] = result.get("parsed")
            else:
                attempt_record["error"] = result.get("error")
            attempts.append(attempt_record)
            return result

        initial_yolo = (
            False if tool_policy.get("try_without_yolo_first") else bool(tool_policy.get("yolo_enabled"))
        )
        initial_name = "restricted_no_yolo" if not initial_yolo else "restricted_yolo"
        result = run_attempt(yolo=initial_yolo, attempt_name=initial_name)
        used_yolo_fallback = False

        if (
            not result.get("ok")
            and not initial_yolo
            and tool_policy.get("yolo_enabled")
            and tool_policy.get("yolo_fallback_on_permission_error")
            and (result.get("error") == "timeout" or needs_yolo_retry(result))
        ):
            result = run_attempt(yolo=True, attempt_name="restricted_yolo_fallback")
            used_yolo_fallback = True

        record = {
            "task_id": task["id"],
            "tool_policy": tool_policy,
            "attempts": attempts,
            "used_yolo_fallback": used_yolo_fallback,
            "ok": bool(result.get("ok")),
            "command": result.get("command"),
            "exit_code": result.get("exit_code"),
            "session_id": result.get("session_id"),
            "text": result.get("text"),
            "stderr": result.get("stderr"),
        }
        if result.get("ok"):
            record["writer_result"] = result.get("parsed")
        else:
            record["error"] = result.get("error")
        invocations.append(record)

    successful = [item for item in invocations if item.get("ok")]
    failed = [item for item in invocations if not item.get("ok")]
    successful_yolo = sum(
        1 for item in successful for attempt in item.get("attempts", []) if attempt.get("ok") and attempt.get("yolo")
    )
    successful_non_yolo = sum(
        1 for item in successful for attempt in item.get("attempts", []) if attempt.get("ok") and not attempt.get("yolo")
    )
    return {
        "execution_mode": "qodercli_headless",
        "executor": "qodercli",
        "real_execution": bool(successful),
        "allowed_write_roots": allowed,
        "tool_policy": tool_policy,
        "task_cards": task_cards,
        "invocations": invocations,
        "successful_invocations": len(successful),
        "failed_invocations": len(failed),
        "successful_non_yolo_invocations": successful_non_yolo,
        "successful_yolo_invocations": successful_yolo,
        "all_invocations_succeeded": not failed,
    }


def run_review_stage(
    request: str,
    plan: Dict[str, Any],
    review: Dict[str, Any],
    tests: Dict[str, Any],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    diff_text = git_diff_text(review.get("changed", []))
    result = invoke_qoder_json(
        prompt=build_review_prompt(request, plan, review, tests, diff_text),
        workspace=REPO_ROOT,
        disallowed_tools=["Edit"],
        max_turns=int(config.get("qoder_review_max_turns", 16)),
        timeout=int(config.get("qoder_timeout_seconds", 1800)),
    )
    if not result.get("ok"):
        return {"ok": False, "invocation": result, "diff_text": diff_text}
    parsed = result.get("parsed")
    if not isinstance(parsed, dict):
        return {
            "ok": False,
            "invocation": result,
            "diff_text": diff_text,
            "error": "reviewer_did_not_return_object",
        }
    return {
        "ok": True,
        "diff_text": diff_text,
        "decision": parsed.get("decision"),
        "summary": parsed.get("summary"),
        "blocking_issues": list(parsed.get("blocking_issues") or []),
        "non_blocking_suggestions": list(parsed.get("non_blocking_suggestions") or []),
        "invocation": result,
    }


def audit(write: Dict[str, Any], tests: Dict[str, Any], review: Dict[str, Any], reviewer: Dict[str, Any]) -> Dict[str, Any]:
    checks = [
        {
            "name": "qoder_native_execution",
            "passed": write.get("execution_mode") == "qodercli_headless",
            "detail": write.get("execution_mode"),
        },
        {
            "name": "restricted_tools_configured",
            "passed": bool((write.get("tool_policy") or {}).get("allowed_tools")),
            "detail": write.get("tool_policy"),
        },
        {
            "name": "write_stage_succeeded",
            "passed": bool(write.get("all_invocations_succeeded")),
            "detail": {
                "successful_invocations": write.get("successful_invocations"),
                "failed_invocations": write.get("failed_invocations"),
            },
        },
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
            "name": "changes_detected",
            "passed": bool(review.get("changed")),
            "detail": {"changed_count": len(review.get("changed", []))},
        },
        {
            "name": "review_approved",
            "passed": reviewer.get("decision") == "approve",
            "detail": reviewer.get("decision"),
        },
    ]
    all_passed = all(check["passed"] for check in checks)
    return {
        "checks": checks,
        "all_passed": all_passed,
        "final_decision": "pass" if all_passed else "fail",
    }


def assemble_report(
    *,
    request: str,
    config: Dict[str, Any],
    preflight: Dict[str, Any],
    plan: Dict[str, Any] | None,
    write: Dict[str, Any] | None,
    tests: Dict[str, Any] | None,
    review: Dict[str, Any] | None,
    reviewer: Dict[str, Any] | None,
    audit_report: Dict[str, Any] | None,
    checkpoint: Dict[str, Any] | None,
    git_context: Dict[str, Any],
    auto_write_guardrail: Dict[str, Any],
    delivery_status: str,
    stage_status: Dict[str, str],
    total_duration_s: float,
) -> Dict[str, Any]:
    return {
        "schema_version": 2,
        "generated_at": utc_now(),
        "repo_root": str(REPO_ROOT),
        "executor": "qodercli",
        "user_request": request,
        "config_used": {
            "project_type": config.get("project_type"),
            "test_preset": config.get("test_preset"),
            "test_command": config.get("test_command"),
            "allowed_write_roots": config.get("allowed_write_roots"),
            "allow_dirty_repo": config.get("allow_dirty_repo"),
            "qoder_cli": config.get("qoder_cli"),
            "qoder_headless_args": config.get("qoder_headless_args"),
            "qoder_write_allowed_tools": config.get("qoder_write_allowed_tools"),
            "qoder_write_disallowed_tools": config.get("qoder_write_disallowed_tools"),
            "qoder_write_try_without_yolo_first": config.get("qoder_write_try_without_yolo_first"),
            "qoder_write_no_yolo_timeout_seconds": config.get("qoder_write_no_yolo_timeout_seconds"),
            "qoder_write_yolo": config.get("qoder_write_yolo"),
            "qoder_write_yolo_fallback_on_permission_error": config.get(
                "qoder_write_yolo_fallback_on_permission_error"
            ),
            "guardrail_max_request_chars": config.get("guardrail_max_request_chars"),
            "guardrail_max_planned_tasks": config.get("guardrail_max_planned_tasks"),
            "guardrail_max_changed_files": config.get("guardrail_max_changed_files"),
            "guardrail_broad_request_markers": config.get("guardrail_broad_request_markers"),
        },
        "git_context": git_context,
        "auto_write_guardrail": auto_write_guardrail,
        "stages": {
            "preflight": preflight,
            "plan": plan,
            "write": write,
            "tests": tests,
            "review": review,
            "reviewer": reviewer,
            "audit": audit_report,
        },
        "checkpoint": checkpoint,
        "stage_status": stage_status,
        "delivery_status": delivery_status,
        "next_step": "python scripts/verify_delivery.py --json --strict",
        "total_duration_s": total_duration_s,
    }


def seal_delivery(report: Dict[str, Any]) -> None:
    DELIVERY_REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Run the Qoder-native self-supervisor.")
    parser.add_argument("--request", default=None, help="natural-language request")
    parser.add_argument("--request-file", default=None, help="read request text from a file")
    parser.add_argument("--force", action="store_true", help="continue despite preflight blockers")
    parser.add_argument("--json", action="store_true", help="also emit the final report JSON")
    args = parser.parse_args(argv)

    if not args.request and not args.request_file:
        print("error: provide --request or --request-file", file=sys.stderr)
        return 2

    if args.request_file:
        try:
            request = Path(args.request_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"error: cannot read request file: {exc}", file=sys.stderr)
            return 2
    else:
        request = str(args.request or "").strip()

    if not request:
        print("error: request is empty", file=sys.stderr)
        return 2

    config = load_config()
    ensure_dirs()
    started = time.monotonic()
    stage_status: Dict[str, str] = {}
    git_context = git_path_info()

    preflight = run_preflight()
    stage_status["preflight"] = "ok" if preflight.get("ready") else ("warn" if args.force else "blocked")
    if not preflight.get("ready") and not args.force:
        report = assemble_report(
            request=request,
            config=config,
            preflight=preflight,
            plan=None,
            write=None,
            tests=None,
            review=None,
            reviewer=None,
            audit_report=None,
            checkpoint=None,
            git_context=git_context,
            auto_write_guardrail=assess_auto_write_guardrail(request, None, None, config),
            delivery_status="blocked_preflight",
            stage_status=stage_status,
            total_duration_s=round(time.monotonic() - started, 3),
        )
        seal_delivery(report)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"[orchestrator] blocked at preflight. Report: {DELIVERY_REPORT}")
        return 1

    ignore = list(config.get("ignore_paths") or [])
    allowed = compute_write_scope(config)
    checkpoint = capture_checkpoint(allowed, ignore)

    snapshot_before = (
        snapshot_repo(REPO_ROOT, ignore)
        if checkpoint.get("review_source") == "filesystem_snapshot"
        else {}
    )

    plan_stage = run_plan_stage(request, config)
    if not plan_stage.get("ok"):
        stage_status["plan"] = "blocked"
        report = assemble_report(
            request=request,
            config=config,
            preflight=preflight,
            plan={"error": plan_stage},
            write=None,
            tests=None,
            review=None,
            reviewer=None,
            audit_report=None,
            checkpoint=checkpoint,
            git_context=git_context,
            auto_write_guardrail=assess_auto_write_guardrail(request, None, None, config),
            delivery_status="blocked_plan",
            stage_status=stage_status,
            total_duration_s=round(time.monotonic() - started, 3),
        )
        seal_delivery(report)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"[orchestrator] blocked at plan. Report: {DELIVERY_REPORT}")
        return 1

    plan = plan_stage["plan"]
    stage_status["plan"] = "ok"

    write = run_write_stage(plan, config, allowed)
    stage_status["write"] = "ok" if write.get("all_invocations_succeeded") else "blocked"

    snapshot_after = (
        snapshot_repo(REPO_ROOT, ignore)
        if checkpoint.get("review_source") == "filesystem_snapshot"
        else {}
    )

    tests = run_unified_tests()
    stage_status["tests"] = "ok" if tests.get("passed") else "blocked"

    review = review_changes(
        before=snapshot_before,
        after=snapshot_after,
        checkpoint=checkpoint,
        ignore=ignore,
        allowed=allowed,
    )
    stage_status["review"] = "ok" if review.get("scope_respected") else "blocked"

    reviewer = run_review_stage(request, plan, review, tests, config)
    stage_status["reviewer"] = "ok" if reviewer.get("ok") else "blocked"

    audit_report = audit(write, tests, review, reviewer if reviewer.get("ok") else {})
    stage_status["audit"] = "ok" if audit_report.get("all_passed") else "blocked"
    auto_write_guardrail = assess_auto_write_guardrail(request, plan, review, config)

    delivery_status = "sealed" if audit_report.get("all_passed") else "blocked"
    report = assemble_report(
        request=request,
        config=config,
        preflight=preflight,
        plan=plan,
        write=write,
        tests=tests,
        review=review,
        reviewer=reviewer,
        audit_report=audit_report,
        checkpoint=checkpoint,
        git_context=git_context,
        auto_write_guardrail=auto_write_guardrail,
        delivery_status=delivery_status,
        stage_status=stage_status,
        total_duration_s=round(time.monotonic() - started, 3),
    )
    seal_delivery(report)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"[orchestrator] delivery_status={delivery_status}")
        print(f"[orchestrator] execution_mode={write.get('execution_mode')}")
        print(f"[orchestrator] auto_write_guardrail={auto_write_guardrail.get('status')}")
        print(f"[orchestrator] report: {DELIVERY_REPORT}")
        print("[orchestrator] next: python scripts/verify_delivery.py --json --strict")

    return 0 if delivery_status == "sealed" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
