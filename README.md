# Qoder Self-Supervisor v1

This repository implements a Qoder-native self-supervised workflow with
three layers:

1. a deterministic Qoder project command
2. a reusable Qoder project skill
3. Python orchestration scripts

The architecture is intentionally strict:

- all model-driven subcalls use `qodercli`
- planning uses `qodercli -w ... -p ... --output-format=json`
- writing uses `qodercli -w ... -p ... --output-format=json`
- review uses `qodercli -w ... -p ... --output-format=json`
- all tests go through `python3 scripts/run_tests.py`
- no non-Qoder executor is part of the delivery workflow

## Primary Entry Point

The documented primary entry point is the Qoder project command:

- [.qoder/commands/self-supervisor.md](/Users/robert/Desktop/qoder/.qoder/commands/self-supervisor.md)

In a Qoder session opened at the repository root, use the project
command `self-supervisor` and provide the natural-language task you want
handled. The command is the deterministic entry point for real runs.

## Secondary Entry Point

The companion project skill is still available:

- [.qoder/skills/self-supervisor-v1/SKILL.md](/Users/robert/Desktop/qoder/.qoder/skills/self-supervisor-v1/SKILL.md)

Use the skill as reusable project knowledge. Prefer the project command
when you want predictable, repeatable execution.

## Repository Structure

```text
.
├── .qoder/
│   ├── commands/
│   │   └── self-supervisor.md
│   ├── skills/
│   │   └── self-supervisor-v1/
│   │       └── SKILL.md
│   └── state/
├── artifacts/
│   └── .gitkeep
├── scripts/
│   ├── bootstrap_mac.sh
│   ├── clean_state.py
│   ├── preflight.py
│   ├── qoder_invoke.py
│   ├── rollback.py
│   ├── run_self_supervisor_qoder.py
│   ├── run_tests.py
│   └── verify_delivery.py
├── supervisor_config.json
└── tests/
```

## Bootstrap

```bash
bash scripts/bootstrap_mac.sh
```

Bootstrap will:

- initialize git if needed
- create `.venv` if needed
- ensure `pytest` is available
- verify `qodercli` is callable
- ensure required `.qoder/` and `artifacts/` directories exist
- run `python3 scripts/preflight.py --fix`

## Preflight

Run preflight directly when you want a deterministic readiness check:

```bash
python3 scripts/preflight.py --json
python3 scripts/preflight.py --fix --json
```

Preflight checks:

- project root markers
- git repo state
- Python environment
- `qodercli --help`
- `qodercli --version`
- `qodercli -w "$PWD" -p "say hello" --output-format=json`
- unified test entry health
- dirty repo state with temp/generated paths ignored

Ignored temp/generated paths:

- `__pycache__`
- `*.pyc`
- `.venv`
- `artifacts`
- `.qoder/state`

## Unified Test Contract

Every stage uses the same test entry point:

```bash
python3 scripts/run_tests.py
```

You can strengthen it in [supervisor_config.json](/Users/robert/Desktop/qoder/supervisor_config.json).

Default:

```json
"test_command": "python -m pytest -q"
```

Extended example:

```json
"test_command": [
  "bash",
  "-lc",
  "ruff check . && mypy . && python -m pytest -q"
]
```

## Write-Stage Safety Model

The write stage is intentionally layered rather than relying only on
`--yolo`.

Current safety model:

1. The request is narrowed into one or more task cards under `.qoder/state/tasks/`
2. The writer prompt is told to stay inside `allowed_write_roots`
3. Qoder CLI tool access is explicitly restricted to the configured write-stage tools
4. The writer tries a restricted run without `--yolo` first
5. The no-yolo attempt is time-bounded so headless runs do not hang indefinitely
6. If headless execution is blocked by a permission prompt or the bounded attempt times out, the workflow can retry with `--yolo`
7. Unified tests run immediately after writing
8. Scope review checks that changed files stayed inside the allowed roots
9. Strict verify confirms the sealed run was truly Qoder-native

Default write-stage tool policy:

```json
"qoder_write_allowed_tools": ["Bash", "Edit"],
"qoder_write_disallowed_tools": [],
"qoder_write_try_without_yolo_first": true,
"qoder_write_no_yolo_timeout_seconds": 45,
"qoder_write_yolo": true,
"qoder_write_yolo_fallback_on_permission_error": true
```

Interpretation:

- `Bash` is for local inspection and validation
- `Edit` is for in-scope file changes
- `--yolo` is available as a headless fallback, not the first and only safety boundary

## Qoder Invocation Wrapper

[scripts/qoder_invoke.py](/Users/robert/Desktop/qoder/scripts/qoder_invoke.py)
is the reusable wrapper around `qodercli`.

It provides:

- safe capability probes
- workspace selection
- headless invocation
- structured JSON event parsing
- JSON payload extraction from assistant text
- clear failure reporting when Qoder output is invalid

Example:

```bash
python3 scripts/qoder_invoke.py --probe --workspace .
python3 scripts/qoder_invoke.py --workspace . --prompt "say hello"
```

## Recommended First Run For A Clean Repo

Use this when the repository is clean and you want the least surprising
path:

```bash
git status --short
bash scripts/bootstrap_mac.sh
python3 scripts/clean_state.py
python3 scripts/preflight.py --json
python3 scripts/run_tests.py
```

Then, from Qoder in the repo root, invoke the project command
`self-supervisor` with your task.

After the run:

```bash
python3 scripts/verify_delivery.py --json --strict
```

## Recommended Rerun After Changes

Use this when you want a fresh, predictable rerun while preserving the
latest sealed report for reference:

```bash
python3 scripts/clean_state.py
python3 scripts/preflight.py --json
python3 scripts/run_tests.py
```

Then update `artifacts/current_request.md` or provide a new task through
the project command and run the workflow again.

Use a full reset only when you explicitly want to remove the previous
sealed artifacts too:

```bash
python3 scripts/clean_state.py --all
```

## Script Entry Points

The project command is the primary interface, but the scripts remain the
deterministic automation layer.

Direct script entry:

```bash
python3 scripts/run_self_supervisor_qoder.py --request "Add a hello helper"
```

File-based entry:

```bash
printf '%s\n' 'Add a hello helper' > artifacts/current_request.md
python3 scripts/run_self_supervisor_qoder.py --request-file artifacts/current_request.md
```

Useful flags:

- `--force` continues past preflight blockers such as `dirty_repo`
- `--json` prints the full sealed delivery report

## What The Orchestrator Does

[scripts/run_self_supervisor_qoder.py](/Users/robert/Desktop/qoder/scripts/run_self_supervisor_qoder.py)
performs:

1. preflight
2. plan via `qodercli`
3. write via `qodercli`
4. unified tests
5. diff + scope review
6. reviewer stage via `qodercli`
7. deterministic audit
8. seal into `artifacts/delivery_report.json`

## Verifying And Sealing

```bash
python3 scripts/verify_delivery.py --json --strict
```

Verification checks:

- `artifacts/delivery_report.json` exists
- `artifacts/user_acceptance.md` exists
- unified tests pass
- changed files remain inside `allowed_write_roots`
- execution was truly Qoder-native:
  - `executor == "qodercli"`
  - `execution_mode == "qodercli_headless"`
  - `real_execution == true`

Final states:

- `ready_for_acceptance`
- `ready_with_warnings`
- `blocked`

With `--strict`, warnings become blocking.

## Human Review Outputs

Machine-readable report:

- [artifacts/delivery_report.json](/Users/robert/Desktop/qoder/artifacts/delivery_report.json)

Human-readable acceptance checklist:

- [artifacts/user_acceptance.md](/Users/robert/Desktop/qoder/artifacts/user_acceptance.md)

The acceptance file is designed to answer:

- what changed
- what tests passed
- which files to inspect
- which commands to run manually
- which risks still remain

## Cleanup And Predictable Reruns

[scripts/clean_state.py](/Users/robert/Desktop/qoder/scripts/clean_state.py)
clears stale run state while preserving the latest delivery report and
acceptance checklist by default.

Default cleanup removes:

- `.qoder/state/*`
- `artifacts/current_request.md`
- `artifacts/preflight_report.json`
- `artifacts/task-*_scratch.md`

Examples:

```bash
python3 scripts/clean_state.py
python3 scripts/clean_state.py --dry-run --json
python3 scripts/clean_state.py --all
```

When to use it:

- before a rerun when you want fresh state and task cards
- after an interrupted run that left stale `.qoder/state/` files behind
- before demos when you want the new report to be obviously current

## Minimal Realistic Demo Task

This is a good small task for a real first demo:

> Create `docs/reviewer_quickstart.md` with a short checklist for manual
> acceptance: inspect changed files, rerun tests, rerun verify, and note
> remaining risks.

It is small, useful, and exercises the same planning/write/review/verify
path as larger real project tasks.

## Strict End-To-End Demo

```bash
bash scripts/bootstrap_mac.sh
python3 scripts/clean_state.py --all
python3 scripts/preflight.py --json
python3 scripts/run_tests.py
printf '%s\n' 'Create docs/reviewer_quickstart.md with a short manual acceptance checklist for this repository. Include: inspect changed files, run python3 scripts/run_tests.py, run python3 scripts/verify_delivery.py --json --strict, and review remaining risks.' > artifacts/current_request.md
python3 scripts/run_self_supervisor_qoder.py --force --request-file artifacts/current_request.md
python3 scripts/verify_delivery.py --json --strict
```

Healthy signals:

- Qoder capability probes pass in preflight
- the write stage reports restricted tools and a real Qoder invocation
- `delivery_status` is `sealed`
- strict verify returns `ready_for_acceptance`

## Rollback

If a write run goes badly, inspect the checkpoint in
`.qoder/state/checkpoint.json` and use:

```bash
python3 scripts/rollback.py --help
```

Rollback is intentionally explicit and separate from the normal run path.

## Operational Caveat In This Environment

In the current Codex desktop environment, some headless `qodercli` calls
may need escalation because Qoder writes log files under:

- `~/.qoder/logs`

This is an environment constraint of the host sandbox, not a fallback
path in the repository. The workflow itself remains Qoder-native and
does not substitute another executor when Qoder is unavailable.
