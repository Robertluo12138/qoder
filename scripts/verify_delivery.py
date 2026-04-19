#!/usr/bin/env python3
"""Verify that a Qoder-native delivery is ready for acceptance."""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
ARTIFACTS = REPO_ROOT / "artifacts"
DELIVERY_REPORT = ARTIFACTS / "delivery_report.json"
USER_ACCEPTANCE = ARTIFACTS / "user_acceptance.md"


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_delivery_report() -> Dict[str, Any]:
    if not DELIVERY_REPORT.exists():
        return {}
    try:
        return json.loads(DELIVERY_REPORT.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def rerun_tests() -> Dict[str, Any]:
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
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "passed": False,
            "status": "invalid_json",
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }


def _inside(path: str, roots: List[str]) -> bool:
    for root in roots:
        root = root.rstrip("/")
        if path == root or path.startswith(root + "/"):
            return True
    return False


def scope_ok(changed: List[str], allowed_roots: List[str]) -> bool:
    if not allowed_roots:
        return True
    return all(_inside(path, allowed_roots) for path in changed)


def classify_final_status(issues: List[str], warnings: List[str], strict: bool) -> str:
    if issues:
        return "blocked"
    if warnings and strict:
        return "blocked"
    if warnings:
        return "ready_with_warnings"
    return "ready_for_acceptance"


def writer_summaries(report: Dict[str, Any]) -> List[str]:
    write = (report.get("stages") or {}).get("write") or {}
    summaries: List[str] = []
    for invocation in write.get("invocations", []) or []:
        result = invocation.get("writer_result") or {}
        summary = result.get("summary")
        if isinstance(summary, str) and summary.strip() and summary not in summaries:
            summaries.append(summary.strip())
    return summaries


def request_summary(report: Dict[str, Any]) -> str:
    request = (report.get("user_request") or "").strip()
    if not request:
        return "(none)"
    first_line = request.splitlines()[0].strip()
    return first_line or request


def manual_validation_commands(changed: List[str]) -> List[str]:
    commands = [
        "python3 scripts/run_tests.py",
        "python3 scripts/verify_delivery.py --json --strict",
        "git status --short",
    ]
    if changed:
        quoted = " ".join(shlex.quote(path) for path in changed)
        commands.append(f"git status --short -- {quoted}")
        commands.append(f"git diff --stat -- {quoted}")
    return commands


def manual_validation_steps(report: Dict[str, Any], changed: List[str]) -> List[str]:
    steps = ["Inspect the changed files listed below."]
    if changed:
        quoted = ", ".join(changed)
        steps.append(f"Compare the resulting file set against the requested scope: {quoted}.")
    for command in manual_validation_commands(changed):
        steps.append(f"Run `{command}`.")
    return steps


def remaining_risks(report: Dict[str, Any], issues: List[str], warnings: List[str]) -> List[str]:
    risks: List[str] = []
    for issue in issues:
        if issue not in risks:
            risks.append(issue)
    for warning in warnings:
        if warning not in risks:
            risks.append(warning)

    checkpoint = report.get("checkpoint") or {}
    if checkpoint.get("was_dirty"):
        risks.append(
            "This run started from a dirty worktree, so changed-file attribution relies on the recorded checkpoint."
        )

    reviewer = (report.get("stages") or {}).get("reviewer") or {}
    for suggestion in reviewer.get("non_blocking_suggestions", []) or []:
        if suggestion not in risks:
            risks.append(suggestion)

    guardrail = report.get("auto_write_guardrail") or {}
    if guardrail.get("status") == "not_recommended_for_unattended_auto_write":
        for reason in guardrail.get("reasons", []) or []:
            if reason not in risks:
                risks.append(reason)

    if not risks:
        risks.append("No known remaining risks beyond normal manual review.")
    return risks


def rollback_guidance(report: Dict[str, Any]) -> List[str]:
    lines = [
        "Review `.qoder/state/checkpoint.json` before reverting anything.",
        "Use `python3 scripts/rollback.py --help` to inspect the supported rollback path.",
        "Prefer reverting or abandoning the isolated branch/worktree rather than manually undoing many files.",
    ]
    git_context = report.get("git_context") or {}
    if git_context.get("is_worktree"):
        lines.append("This run was executed from a git worktree; if you discard the run, remove the worktree after review.")
    return lines


def build_acceptance_md(
    report: Dict[str, Any],
    rerun: Dict[str, Any],
    final_status: str,
    issues: List[str],
    warnings: List[str],
) -> str:
    plan = (report.get("stages") or {}).get("plan") or {}
    review = (report.get("stages") or {}).get("review") or {}
    tests = (report.get("stages") or {}).get("tests") or {}
    reviewer = (report.get("stages") or {}).get("reviewer") or {}
    audit = (report.get("stages") or {}).get("audit") or {}
    write = (report.get("stages") or {}).get("write") or {}
    changed = review.get("changed", []) or []
    summaries = writer_summaries(report)
    commands = manual_validation_commands(changed)
    steps = manual_validation_steps(report, changed)
    risks = remaining_risks(report, issues, warnings)
    rollback = rollback_guidance(report)
    git_context = report.get("git_context") or {}
    guardrail = report.get("auto_write_guardrail") or {}

    lines: List[str] = []
    lines.append("# User Acceptance — Qoder Self-Supervisor Delivery")
    lines.append("")
    lines.append(f"- generated_at: {utc_now()}")
    lines.append(f"- delivery_status: {report.get('delivery_status', 'unknown')}")
    lines.append(f"- verification_status: **{final_status}**")
    lines.append(f"- report: `artifacts/delivery_report.json`")
    lines.append("")
    lines.append("## Request Summary")
    lines.append("")
    lines.append(f"- summary: {request_summary(report)}")
    if git_context:
        lines.append(f"- branch: `{git_context.get('branch', 'unknown')}`")
        lines.append(f"- git context: `{'worktree' if git_context.get('is_worktree') else 'branch_or_main_checkout'}`")
    if guardrail:
        lines.append(f"- unattended auto-write: `{guardrail.get('status', 'unknown')}`")
    lines.append("")
    lines.append("## What Changed")
    lines.append("")
    if summaries:
        for summary in summaries:
            lines.append(f"- {summary}")
    else:
        for task in plan.get("tasks", []) or []:
            lines.append(f"- {task.get('title')}")
    if not summaries and not plan.get("tasks"):
        lines.append("- (no change summary recorded)")
    lines.append("")
    lines.append("## Files To Inspect")
    lines.append("")
    if changed:
        for path in changed:
            lines.append(f"- `{path}`")
    else:
        lines.append("- (none)")
    lines.append("")
    lines.append("## Automated Validation Summary")
    lines.append("")
    lines.append(
        f"- write execution: `{write.get('execution_mode', 'unknown')}` via `{write.get('executor', 'unknown')}`"
    )
    if "successful_non_yolo_invocations" in write or "successful_yolo_invocations" in write:
        lines.append(
            f"- write attempts: non-yolo={write.get('successful_non_yolo_invocations', 0)}, "
            f"yolo={write.get('successful_yolo_invocations', 0)}"
        )
    lines.append(
        f"- sealed run: `{' '.join(tests.get('command', [])) or 'python3 scripts/run_tests.py'}` -> "
        f"`{tests.get('status', 'unknown')}` (exit {tests.get('exit_code', '?')})"
    )
    lines.append(
        f"- rerun: `{' '.join(rerun.get('command', [])) or 'python3 scripts/run_tests.py'}` -> "
        f"`{rerun.get('status', 'unknown')}` (exit {rerun.get('exit_code', '?')})"
    )
    lines.append(f"- decision: `{reviewer.get('decision', 'unknown')}`")
    if reviewer.get("summary"):
        lines.append(f"- summary: {reviewer.get('summary')}")
    lines.append(f"- final_decision: `{audit.get('final_decision', 'unknown')}`")
    for check in audit.get("checks", []) or []:
        lines.append(f"- {check.get('name')}: {'pass' if check.get('passed') else 'fail'}")
    lines.append("")
    lines.append("## Manual Validation Steps")
    lines.append("")
    for step in steps:
        lines.append(f"- {step}")
    lines.append("")
    lines.append("## Manual Validation Commands")
    lines.append("")
    for command in commands:
        lines.append(f"- `{command}`")
    lines.append("")
    lines.append("## Remaining Risks")
    lines.append("")
    for risk in risks:
        lines.append(f"- {risk}")
    lines.append("")
    lines.append("## Rollback Guidance")
    lines.append("")
    for item in rollback:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## Acceptance Decision")
    lines.append("")
    lines.append(f"- verification_status: `{final_status}`")
    if final_status == "ready_for_acceptance":
        lines.append("- Recommended next step: inspect the files above, run the manual validation commands, then accept if the result matches the request.")
    elif final_status == "ready_with_warnings":
        lines.append("- Warnings are present but non-blocking; inspect the risks section before accepting.")
    else:
        lines.append("- Acceptance is blocked until the listed issues are resolved.")
    lines.append("")
    return "\n".join(lines)


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Verify a sealed Qoder-native delivery.")
    parser.add_argument("--json", action="store_true", help="emit JSON summary")
    parser.add_argument("--strict", action="store_true", help="treat warnings as blocking")
    args = parser.parse_args(argv)

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    issues: List[str] = []
    warnings: List[str] = []

    report = load_delivery_report()
    if not report:
        issues.append("delivery_report.json is missing or unreadable")

    rerun = rerun_tests()
    if not rerun.get("passed"):
        issues.append(
            f"unified test entry failed on rerun (status={rerun.get('status')}, exit_code={rerun.get('exit_code')})"
        )

    allowed = []
    changed: List[str] = []
    if report:
        allowed = list((report.get("config_used") or {}).get("allowed_write_roots") or [])
        changed = list((((report.get("stages") or {}).get("review") or {}).get("changed")) or [])

    if changed and allowed and not scope_ok(changed, allowed):
        issues.append("changed files are outside allowed_write_roots")

    delivery_status = report.get("delivery_status", "unknown") if report else "missing"
    if delivery_status != "sealed":
        issues.append(f"delivery_status is '{delivery_status}', expected 'sealed'")

    write = ((report.get("stages") or {}).get("write") or {}) if report else {}
    if write.get("executor") != "qodercli":
        issues.append("write stage was not qodercli-native")
    if write.get("execution_mode") != "qodercli_headless":
        issues.append(
            f"execution_mode is '{write.get('execution_mode', 'unknown')}', expected 'qodercli_headless'"
        )
    if not write.get("real_execution"):
        issues.append("write stage did not record a real execution")

    sealed_status = (((report.get("stages") or {}).get("tests") or {}).get("status")) if report else None
    if sealed_status and rerun.get("status") and sealed_status != rerun.get("status"):
        warnings.append(
            f"test status drift: sealed='{sealed_status}' vs rerun='{rerun.get('status')}'"
        )

    final_status = classify_final_status(issues, warnings, args.strict)
    USER_ACCEPTANCE.write_text(
        build_acceptance_md(report, rerun, final_status, issues, warnings),
        encoding="utf-8",
    )

    if not USER_ACCEPTANCE.exists():
        issues.append("user_acceptance.md could not be written")
        final_status = classify_final_status(issues, warnings, args.strict)

    summary = {
        "schema_version": 2,
        "final_status": final_status,
        "issues": issues,
        "warnings": warnings,
        "request_summary": request_summary(report),
        "auto_write_guardrail": (report.get("auto_write_guardrail") or {}).get("status") if report else None,
        "git_context": report.get("git_context") if report else None,
        "remaining_risks": remaining_risks(report, issues, warnings),
        "rollback_guidance": rollback_guidance(report),
        "manual_validation_steps": manual_validation_steps(report, changed),
        "manual_validation_commands": manual_validation_commands(changed),
        "rerun_tests": rerun,
        "delivery_report_exists": bool(report),
        "user_acceptance_exists": USER_ACCEPTANCE.exists(),
        "delivery_status": delivery_status,
        "execution_mode": write.get("execution_mode"),
        "user_acceptance_md": str(USER_ACCEPTANCE.relative_to(REPO_ROOT)),
    }

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"[verify] final_status: {final_status}")
        print(f"[verify] user acceptance: {USER_ACCEPTANCE}")
        if issues:
            print("[verify] issues:")
            for issue in issues:
                print(f"  - {issue}")
        if warnings:
            print("[verify] warnings:")
            for warning in warnings:
                print(f"  - {warning}")

    return 0 if final_status in ("ready_for_acceptance", "ready_with_warnings") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
