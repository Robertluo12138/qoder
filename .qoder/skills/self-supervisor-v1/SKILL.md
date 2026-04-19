---
name: self-supervisor-v1
description: Qoder-native end-to-end delivery workflow. Use when the user wants planning, implementation, testing, review, audit, and final acceptance artifacts driven by qodercli headless mode plus local Python orchestration.
version: 1.1.0
---

# self-supervisor-v1

This project skill describes when to use the local Qoder-native
self-supervisor workflow. The primary deterministic entry point is the
project command at `.qoder/commands/self-supervisor.md`.

## When to use this skill

Use this workflow when the user wants:

- a natural-language task handled end to end,
- implementation driven through `qodercli`,
- a single unified test contract,
- a machine-readable delivery report,
- a human-readable acceptance checklist.

Do not use it for:

- simple Q&A,
- one-off explanations,
- tasks that do not need code or file changes,
- environments where `qodercli` is missing or its headless mode is not
  working.

## Execution Model

The workflow has three layers:

1. `.qoder/commands/self-supervisor.md` as the deterministic entry point
2. this Skill as reusable repository knowledge
3. Python orchestration under `scripts/`

## Local Entry Points

Primary orchestrator:

```bash
python3 scripts/run_self_supervisor_qoder.py --request "<user request>"
```

Verification:

```bash
python3 scripts/verify_delivery.py --json --strict
```

## Required Contract

- planning uses `qodercli -w ... -p ... --output-format=json`
- writing uses `qodercli -w ... -p ... --output-format=json --yolo`
- review uses `qodercli -w ... -p ... --output-format=json`
- all testing goes through `python3 scripts/run_tests.py`
- no non-Qoder executor is allowed

## Outputs

- `artifacts/delivery_report.json`
- `artifacts/user_acceptance.md`
- `.qoder/state/plan.json`
- `.qoder/state/checkpoint.json`
- `.qoder/state/tasks/*.md`
