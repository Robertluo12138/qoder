#!/usr/bin/env python3
"""Verify that a sealed delivery is ready for human acceptance.

What this script does, in order:

1. Confirm ``artifacts/delivery_report.json`` exists and parses.
2. Re-run the unified test entry point (``scripts/run_tests.py``) and
   compare the status to the sealed run to catch environmental drift.
3. Re-check that every file recorded in the report's review stage is
   inside ``allowed_write_roots``.
4. Emit a human-readable ``artifacts/user_acceptance.md`` with the
   exact next steps for accepting or rejecting the delivery.
5. Return one of a small, well-defined set of statuses.

CLI:

    --json     emit a structured JSON summary on stdout
    --strict   treat warnings as failures (non-zero exit)

Exit codes:

    0   ready_for_acceptance  (or ready_with_warnings unless --strict)
    1   blocked (missing report, test regression, scope violation)
    2   warn_strict (warnings + --strict)
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
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
    """Read and parse the sealed delivery report. Returns ``{}`` on failure."""
    if not DELIVERY_REPORT.exists():
        return {}
    try:
        return json.loads(DELIVERY_REPORT.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def rerun_tests() -> Dict[str, Any]:
    """Re-run the unified tests. Output is the same JSON schema as run_tests.py."""
    cmd = [sys.executable, str(SCRIPT_DIR / "run_tests.py")]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return {"passed": False, "status": "timeout", "exit_code": 124}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "passed": False,
            "status": "error",
            "raw_stdout": proc.stdout,
            "raw_stderr": proc.stderr,
        }


def _inside(path: str, roots: List[str]) -> bool:
    for r in roots:
        r_norm = r.rstrip("/")
        if path == r_norm or path.startswith(r_norm + "/"):
            return True
    return False


def scope_ok(changed: List[str], allowed_roots: List[str]) -> bool:
    """All changed files must live under at least one allowed root."""
    if not allowed_roots:
        return True
    return all(_inside(p, allowed_roots) for p in changed)


def build_acceptance_md(
    report: Dict[str, Any],
    rerun: Dict[str, Any],
    status: str,
    issues: List[str],
    warnings: List[str],
) -> str:
    """Assemble the human-readable acceptance checklist."""
    plan = (report.get("stages", {}) or {}).get("plan") or {}
    review = (report.get("stages", {}) or {}).get("review") or {}
    tests_sealed = (report.get("stages", {}) or {}).get("tests") or {}
    write = (report.get("stages", {}) or {}).get("write") or {}

    lines: List[str] = []
    lines.append("# User Acceptance — Self-Supervisor Delivery")
    lines.append("")
    lines.append(f"- generated_at: {utc_now()}")
    lines.append(f"- delivery_status: {report.get('delivery_status', 'unknown')}")
    lines.append(f"- verification_status: **{status}**")
    lines.append(f"- report: `artifacts/delivery_report.json`")
    lines.append("")

    lines.append("## Request")
    lines.append("")
    req = (report.get("user_request") or "").strip().replace("\n", "\n> ")
    lines.append("> " + (req or "(none)"))
    lines.append("")

    lines.append("## Plan")
    lines.append("")
    lines.append(f"- mode: {plan.get('mode', 'unknown')}")
    lines.append("- tasks:")
    for t in plan.get("tasks", []) or []:
        lines.append(f"  - {t.get('id')}: {t.get('title')}")
    if not plan.get("tasks"):
        lines.append("  - (no tasks recorded)")
    lines.append("")

    lines.append("## Files changed")
    lines.append("")
    changed = review.get("changed", []) or []
    if changed:
        for c in changed:
            lines.append(f"- `{c}`")
    else:
        lines.append("- (none — either a no-op run or advisory mode)")
    lines.append("")

    lines.append("## Test evidence")
    lines.append("")
    lines.append(
        f"- sealed run: `{tests_sealed.get('status', 'unknown')}` "
        f"(exit {tests_sealed.get('exit_code', '?')})"
    )
    lines.append(
        f"- rerun    : `{rerun.get('status', 'unknown')}` "
        f"(exit {rerun.get('exit_code', '?')})"
    )
    same = tests_sealed.get("status") == rerun.get("status")
    lines.append(f"- consistency: {'identical' if same else 'DRIFT'}")
    lines.append("")

    if write.get("implementation_mode") in ("fallback_advisory", "dry_run"):
        lines.append("## Advisory mode notice")
        lines.append("")
        lines.append(
            "The qoder CLI was unavailable or no `qoder_exec_template` was "
            "configured, so the orchestrator did not modify source code. "
            "It wrote task cards under `.qoder/state/tasks/` and scratch "
            "notes under `artifacts/` describing the pending work."
        )
        lines.append("")

    lines.append("## What to do next")
    lines.append("")
    if status == "ready_for_acceptance":
        lines.append("1. Inspect the file list above for accuracy.")
        lines.append("2. Open `artifacts/delivery_report.json` for the full stage-by-stage evidence.")
        lines.append("3. If the outcome matches your request, commit and proceed.")
        lines.append(
            "4. If it does not, re-run "
            "`python scripts/run_self_supervisor_qoder.py --request \"…\"` "
            "with revised instructions."
        )
    elif status == "ready_with_warnings":
        lines.append("Delivery is acceptable but has non-blocking warnings:")
        for w in warnings:
            lines.append(f"- {w}")
        lines.append("")
        lines.append("If the warnings are acceptable to you, proceed.")
        lines.append("Otherwise, re-run the orchestrator with revised instructions.")
    else:
        lines.append("**Acceptance is not recommended yet.** Issues detected:")
        for i in issues:
            lines.append(f"- {i}")
        for w in warnings:
            lines.append(f"- (warning) {w}")
        lines.append("")
        lines.append(
            "Resolve the issues, then re-run "
            "`python scripts/run_self_supervisor_qoder.py` and re-verify."
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Verify a sealed self-supervisor delivery.",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit a JSON summary on stdout"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="non-zero exit on any warning, not just blocking issues",
    )
    args = parser.parse_args(argv)

    ARTIFACTS.mkdir(parents=True, exist_ok=True)

    issues: List[str] = []
    warnings: List[str] = []

    report = load_delivery_report()
    if not report:
        issues.append(
            "delivery_report.json is missing or unreadable — run "
            "`scripts/run_self_supervisor_qoder.py` first."
        )

    rerun = rerun_tests()
    if not rerun.get("passed"):
        issues.append(
            f"test rerun failed (status={rerun.get('status')}, "
            f"exit_code={rerun.get('exit_code')})"
        )

    # Scope check using the delivery report's recorded review output.
    review = (report.get("stages", {}) or {}).get("review", {}) if report else {}
    changed = review.get("changed", []) or []
    allowed = None
    if report:
        allowed = report.get("config_used", {}).get("allowed_write_roots")
        if allowed is None:
            allowed = (report.get("stages", {}) or {}).get("write", {}).get(
                "allowed_write_roots", []
            )
    if changed and allowed and not scope_ok(changed, allowed):
        issues.append(
            "scope violation: some changed files are outside allowed_write_roots"
        )

    # Consistency between sealed tests and this re-run.
    sealed_status = (
        (report.get("stages", {}) or {}).get("tests", {}).get("status") if report else None
    )
    if sealed_status and rerun.get("status") and sealed_status != rerun.get("status"):
        warnings.append(
            f"test status drift: sealed='{sealed_status}' vs rerun='{rerun.get('status')}'"
        )

    # Delivery status sanity.
    delivery_status = report.get("delivery_status", "unknown") if report else "missing"
    if delivery_status not in ("sealed", "sealed_advisory"):
        warnings.append(
            f"delivery_status is '{delivery_status}' (expected 'sealed' or 'sealed_advisory')"
        )
    elif delivery_status == "sealed_advisory":
        warnings.append(
            "delivery was sealed in advisory mode — no source code changes were applied automatically"
        )

    # Classify final status
    if issues:
        status = "blocked"
    elif warnings and args.strict:
        status = "warn_strict"
    elif warnings:
        status = "ready_with_warnings"
    else:
        status = "ready_for_acceptance"

    md = build_acceptance_md(report, rerun, status, issues, warnings)
    USER_ACCEPTANCE.write_text(md, encoding="utf-8")

    summary = {
        "schema_version": 1,
        "status": status,
        "issues": issues,
        "warnings": warnings,
        "rerun_tests": rerun,
        "delivery_report_exists": bool(report),
        "delivery_status": delivery_status,
        "user_acceptance_md": str(USER_ACCEPTANCE.relative_to(REPO_ROOT)),
    }

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"[verify] status: {status}")
        print(f"[verify] user acceptance: {USER_ACCEPTANCE}")
        if issues:
            print("[verify] issues:")
            for i in issues:
                print(f"  - {i}")
        if warnings:
            print("[verify] warnings:")
            for w in warnings:
                print(f"  - {w}")

    if status == "ready_for_acceptance":
        return 0
    if status == "ready_with_warnings":
        return 0
    if status == "warn_strict":
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
