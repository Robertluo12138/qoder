# Qoder Self-Supervisor v1

A self-supervised development workflow for Qoder CLI projects. Give it a
task in natural language and it handles **plan → write → test → review →
audit → seal**, producing a machine-verifiable delivery package so you
can accept or reject the work with confidence.

## What you get

- `artifacts/delivery_report.json` — stage-by-stage structured evidence.
- `artifacts/user_acceptance.md`   — human-readable acceptance checklist.
- `.qoder/state/plan.json`         — the plan that was actually used.
- `.qoder/state/tasks/*.md`        — one card per planned sub-task.

## Layout

```
.
├── README.md
├── supervisor_config.json
├── .gitignore
├── .qoder/
│   └── skills/
│       └── self-supervisor-v1/
│           └── SKILL.md
├── artifacts/
│   └── .gitkeep
├── scripts/
│   ├── bootstrap_mac.sh
│   ├── preflight.py
│   ├── run_tests.py
│   ├── run_self_supervisor_qoder.py
│   └── verify_delivery.py
└── tests/
    └── test_smoke.py
```

## 1. Install into a Qoder CLI project

**Option A — drop in alongside an existing project (recommended).**

```bash
# From the destination project's root:
cp -R /path/to/qoder-self-supervisor/. ./
bash scripts/bootstrap_mac.sh
```

If the destination already has a `.gitignore`, `supervisor_config.json`,
or `scripts/` directory, review the incoming files first — the
workflow's scripts live under `scripts/` and should not clash with
existing names.

**Option B — use this directory as the project root.**

`scripts/bootstrap_mac.sh` will:

- initialise a git repo (on branch `main` when supported) if one is
  missing,
- create `.venv/` and install `pytest` into it,
- ensure `artifacts/` and `.qoder/state/tasks/` exist,
- run `preflight.py --fix` to normalise `.gitignore`.

The only hard requirements are **Python 3.9+** and **bash**. The
`qoder` CLI is optional — the workflow degrades to **advisory mode** if
it isn't on `PATH` or no `qoder_exec_template` is configured.

## 2. Use it as a global Qoder Skill

The skill file at `.qoder/skills/self-supervisor-v1/SKILL.md` follows
the standard Qoder Skill format (YAML frontmatter + body):

```yaml
---
name: self-supervisor-v1
description: …
---
```

Two usage modes:

- **Project-scoped** (default): the skill sits under the project's
  `.qoder/skills/` and is auto-discovered by the local Qoder CLI.
- **Global**: copy the skill into the user-level skills directory once:

  ```bash
  mkdir -p ~/.qoder/skills
  cp -R .qoder/skills/self-supervisor-v1 ~/.qoder/skills/
  ```

  After that, any project that also contains the `scripts/` suite and a
  `supervisor_config.json` can invoke the skill by name. The skill
  expects to find `scripts/run_self_supervisor_qoder.py` relative to
  the current working directory.

## 3. Validate that the workflow is healthy

Run preflight and the unified test runner:

```bash
python scripts/preflight.py --fix     # first time
python scripts/preflight.py           # subsequent runs
python scripts/run_tests.py
```

Healthy signals:

- preflight JSON has `"ready": true` with an empty `blocking` array.
- `run_tests.py` prints JSON with `"status": "ok"` (tests pass) or
  `"status": "ok_no_tests"` (no tests yet — also acceptable).

Common `blocking` reasons and the fix:

| Reason | Fix |
|---|---|
| `not_a_git_repo` | `bash scripts/bootstrap_mac.sh` |
| `project_root_not_recognized` | create `supervisor_config.json` or init git |
| `python_unavailable` | install Python 3.9+ |
| `dirty_repo` | commit/stash, or set `"allow_dirty_repo": true` |

`qoder_cli_not_callable` is a **warning**, not a blocker — the
orchestrator runs in advisory mode when qoder is missing.

## 4. Run a minimal demo

```bash
python scripts/run_self_supervisor_qoder.py \
  --request "Add a hello() helper that returns 'hello world'"
```

Expected output:

```
[orchestrator] delivery_status=sealed  (or sealed_advisory)
[orchestrator] implementation_mode=qoder_cli  (or fallback_advisory)
[orchestrator] report: /path/to/artifacts/delivery_report.json
[orchestrator] next: python scripts/verify_delivery.py
```

Then verify:

```bash
python scripts/verify_delivery.py --json
```

Expected terminal status: `ready_for_acceptance` (or
`ready_with_warnings` when running in advisory mode).

Useful flags:

- `--request-file path.txt` — load the prompt from a file.
- `--dry-run`               — write the plan and task cards only.
- `--force`                 — proceed past non-ok preflight.
- `--json`                  — emit the full delivery report on stdout.

## 5. Inspecting the outputs

### `artifacts/delivery_report.json`

Structured JSON with a `stages` object that embeds every raw stage
output. Fields you'll check first:

- `delivery_status` — one of `sealed`, `sealed_advisory`,
  `blocked_preflight`, `blocked_audit`.
- `stages.tests` — full test JSON (exit code, status, truncated output).
- `stages.review.changed` — every file the orchestrator modified.
- `stages.audit.checks` — the deterministic pass/fail gates.
- `stage_status` — a flat per-stage status map for quick triage.

### `artifacts/user_acceptance.md`

Human-readable checklist produced by `scripts/verify_delivery.py`.
Tells you:

- the request, plan, and files changed,
- sealed-vs-rerun test comparison (drift detection),
- whether the delivery ran in advisory mode,
- the exact next steps to accept or reject the delivery.

## Configuration (`supervisor_config.json`)

| Field | Purpose |
|---|---|
| `project_type` | Informational tag (e.g. `"python"`). |
| `test_command` | Overrides the default `python -m pytest -q`. String or argv list. |
| `ignore_paths` | Glob/path patterns excluded from "dirty" detection and from the file-diff snapshot. |
| `allow_dirty_repo` | If `true`, preflight does not block on uncommitted changes. |
| `prefer_repo_venv` | If `true`, `run_tests.py` uses `.venv/bin/python` when it exists. |
| `allowed_write_roots` | Scope gate enforced by review, audit, and verify. |
| `qoder_cli` | Name of the qoder executable on `PATH` (default `"qoder"`). |
| `qoder_exec_template` | Optional argv list with `{prompt}`, `{title}`, `{task_id}`, `{card_path}` placeholders. Set to `null` for advisory mode. |
| `single_task_threshold_chars` | Requests shorter than this collapse to a single task unless they contain explicit enumeration. |

Example `qoder_exec_template` to wire in once you know your qoder CLI's
subcommand:

```json
"qoder_exec_template": ["run", "--auto", "--prompt", "{prompt}"]
```

## Mental model

The supervisor is deliberately conservative:

- **Deterministic audit.** Acceptance is not a model judgment — it is
  a scope check plus a test-result check.
- **Separated evidence.** Plan, diff, test log, and audit live in
  distinct fields of the report so drift can be inspected.
- **Idempotent stages.** Re-running is always safe; the orchestrator
  snapshots before and after and reports diffs based on current state.
- **Graceful degradation.** When qoder is missing, tasks become
  advisory rather than silently no-oping.

## Troubleshooting

- **`blocked_preflight`** — inspect `stages.preflight.blocking`. Most
  common cause is a missing git repo; run
  `python scripts/preflight.py --fix`.
- **`blocked_audit`** — inspect `stages.audit.checks`. One of the
  deterministic gates failed. Look at `stages.tests` and
  `stages.review` for specifics.
- **Test status drift** (sealed vs rerun) — environment changed between
  the sealed run and verification (e.g. a package installed or
  removed). Re-bootstrap and re-run the orchestrator.
- **Empty `stages.review.changed` in non-advisory mode** — your
  `qoder_exec_template` ran but made no changes. Check
  `stages.write.invocations[*].exit_code` and `stdout_tail` for the
  real reason.

## License

Internal tooling. Adapt freely.
