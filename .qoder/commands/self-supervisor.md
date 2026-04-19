---
description: Run the Qoder-native self-supervisor workflow for the current request.
---

# self-supervisor

Use this project command as the primary deterministic entry point for
the repository-local, Qoder-native self-supervisor.

## Command Behavior

When invoked, do the following in the current project:

1. Save the user request to `artifacts/current_request.md`.
2. If you want a clean git context first, optionally run `python3 scripts/prepare_isolated_run.py --mode worktree --json` or `--mode branch --json`.
3. If you want a fresh rerun, optionally run `python3 scripts/clean_state.py`.
4. Run `python3 scripts/preflight.py --fix --json`.
5. Run `python3 scripts/run_self_supervisor_qoder.py --request-file artifacts/current_request.md`.
6. Run `python3 scripts/verify_delivery.py --json --strict`.
7. Summarize:
   - the plan
   - changed files
   - test result
   - review decision
   - audit decision
   - verification final status
   - the user acceptance checklist

## Rules

- Use `qodercli` only for internal model-driven stages.
- Do not substitute any non-Qoder backend.
- If `qodercli` is unavailable or headless mode fails, stop and report
  the blocking issue clearly.
- Keep all testing routed through `scripts/run_tests.py`.
