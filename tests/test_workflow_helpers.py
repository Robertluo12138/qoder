"""Targeted tests for the Qoder-native self-supervisor helpers."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relative_path: str):
    path = REPO_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_extract_json_value_handles_fenced_json() -> None:
    qoder = load_module("qoder_invoke_module", "scripts/qoder_invoke.py")
    payload = qoder.extract_json_value("```json\n{\"ok\": true, \"count\": 2}\n```")
    assert payload == {"ok": True, "count": 2}


def test_small_requests_default_to_single_task() -> None:
    orchestrator = load_module("qoder_orchestrator_module", "scripts/run_self_supervisor_qoder.py")
    plan = orchestrator.normalize_plan(
        "Add a hello helper",
        {"single_task_threshold_chars": 200},
        {
            "mode": "multi_task",
            "tasks": [
                {"id": "task-1", "title": "Part 1", "description": "A", "acceptance": ["one"]},
                {"id": "task-2", "title": "Part 2", "description": "B", "acceptance": ["two"]},
            ],
        },
    )
    assert plan["mode"] == "single_task"
    assert len(plan["tasks"]) == 1


def test_verify_strict_blocks_warning_only_runs() -> None:
    verify = load_module("qoder_verify_module", "scripts/verify_delivery.py")
    assert verify.classify_final_status([], ["warning"], strict=False) == "ready_with_warnings"
    assert verify.classify_final_status([], ["warning"], strict=True) == "blocked"


def test_run_tests_json_includes_returncode_alias() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "run_tests.py"),
            "--command",
            "python -c \"print('workflow-ok')\"",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(proc.stdout)
    assert payload["returncode"] == payload["exit_code"] == 0


def test_run_tests_resolves_configured_preset() -> None:
    run_tests = load_module("qoder_run_tests_module", "scripts/run_tests.py")
    cmd, preset = run_tests.resolve_command({"test_preset": "ruff_pytest"})
    assert preset == "ruff_pytest"
    assert cmd[0] == "bash"
    assert "ruff check ." in cmd[-1]


def test_manual_validation_commands_include_git_status_for_changed_files() -> None:
    verify = load_module("qoder_verify_module", "scripts/verify_delivery.py")
    commands = verify.manual_validation_commands(["docs/example.md"])
    assert "python3 scripts/run_tests.py" in commands
    assert any(cmd.startswith("git status --short -- ") for cmd in commands)


def test_request_summary_uses_first_line() -> None:
    verify = load_module("qoder_verify_module", "scripts/verify_delivery.py")
    summary = verify.request_summary({"user_request": "First line\nSecond line"})
    assert summary == "First line"


def test_remaining_risks_mentions_dirty_checkpoint() -> None:
    verify = load_module("qoder_verify_module", "scripts/verify_delivery.py")
    risks = verify.remaining_risks(
        {
            "checkpoint": {"was_dirty": True},
            "stages": {"reviewer": {"non_blocking_suggestions": []}},
        },
        [],
        [],
    )
    assert any("dirty worktree" in risk for risk in risks)


def test_guardrail_marks_broad_request_not_recommended() -> None:
    orchestrator = load_module("qoder_orchestrator_module", "scripts/run_self_supervisor_qoder.py")
    assessment = orchestrator.assess_auto_write_guardrail(
        "Do a large refactor across the codebase and touch all files.",
        {"tasks": [{"id": "task-1"}]},
        {"changed": ["a.py"]},
        {},
    )
    assert assessment["status"] == "not_recommended_for_unattended_auto_write"
    assert assessment["reasons"]


def test_prepare_isolated_run_builds_worktree_plan() -> None:
    helper = load_module("qoder_prepare_isolated_run_module", "scripts/prepare_isolated_run.py")
    plan = helper.build_plan("worktree", "qoder/test-branch", "HEAD", None)
    assert plan["mode"] == "worktree"
    assert "git" in plan["commands"][0][0]
    assert "worktree" in plan["commands"][0]


def test_clean_state_default_candidates_include_transient_artifacts(tmp_path) -> None:
    clean = load_module("qoder_clean_state_module", "scripts/clean_state.py")
    repo = tmp_path / "repo"
    state = repo / ".qoder" / "state"
    tasks = state / "tasks"
    artifacts = repo / "artifacts"
    tasks.mkdir(parents=True)
    artifacts.mkdir(parents=True)

    (artifacts / "current_request.md").write_text("request", encoding="utf-8")
    (artifacts / "preflight_report.json").write_text("{}", encoding="utf-8")
    (artifacts / "delivery_report.json").write_text("{}", encoding="utf-8")
    (artifacts / "user_acceptance.md").write_text("ok", encoding="utf-8")

    clean.REPO_ROOT = repo
    clean.STATE = state
    clean.TASKS = tasks
    clean.ARTIFACTS = artifacts
    clean.DELIVERY_REPORT = artifacts / "delivery_report.json"
    clean.USER_ACCEPTANCE = artifacts / "user_acceptance.md"

    candidates = [clean._rel(path) for path in clean._candidates(remove_delivery=False)]
    assert "artifacts/current_request.md" in candidates
    assert "artifacts/preflight_report.json" in candidates
    assert "artifacts/delivery_report.json" not in candidates
    assert "artifacts/user_acceptance.md" not in candidates
