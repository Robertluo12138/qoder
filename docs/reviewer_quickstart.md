# Reviewer Quickstart Guide

This document provides a quick checklist for reviewers to validate code changes.

## Inspecting Changed Files

Before running tests, inspect the changed files to understand the scope of modifications:

- Review the diff to understand what was modified
- Check for any unexpected file changes
- Verify file permissions are appropriate
- Ensure no sensitive data was accidentally committed

## Running Tests

Execute the test suite to verify all tests pass:

```bash
python3 scripts/run_tests.py
```

## Verifying Delivery

Run the delivery verification script with strict JSON output:

```bash
python3 scripts/verify_delivery.py --json --strict
```

## Final Review

**Important:** Before accepting the changes, review any remaining risks identified during the inspection and testing phases. Ensure all concerns are addressed or documented before final acceptance.
