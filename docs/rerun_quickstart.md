# Rerun Quickstart

This checklist helps you quickly rerun the verification pipeline after making changes.

## Steps

1. **Clean state**
   ```bash
   python3 scripts/clean_state.py
   ```

2. **Run preflight checks**
   ```bash
   python3 scripts/preflight.py --json
   ```

3. **Run tests**
   ```bash
   python3 scripts/run_tests.py
   ```

4. **Verify delivery**
   ```bash
   python3 scripts/verify_delivery.py --json --strict
   ```

## Reviewer Checklist

- [ ] Inspect all changed files for correctness
- [ ] Identify and document any remaining risks
