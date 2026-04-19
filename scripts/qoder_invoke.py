#!/usr/bin/env python3
"""Helpers for invoking qodercli in headless mode.

This module centralizes:

- capability probes for ``qodercli``
- deterministic headless invocation via
  ``qodercli -w ... -p ... --output-format=json``
- structured JSON event parsing
- best-effort extraction of JSON payloads from assistant text
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent


def repo_root_from_script() -> Path:
    return REPO_ROOT


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def run_process(
    cmd: List[str],
    *,
    cwd: Path,
    timeout: int = 1800,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess with captured text output."""
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def parse_json_events(stdout: str) -> List[Dict[str, Any]]:
    """Parse line-delimited Qoder JSON events from stdout."""
    events: List[Dict[str, Any]] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def event_text(events: List[Dict[str, Any]]) -> str:
    """Collect assistant text content from parsed Qoder JSON events."""
    fragments: List[str] = []
    for event in events:
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    fragments.append(text)
    return "\n".join(fragment.strip() for fragment in fragments if fragment.strip()).strip()


def extract_json_value(text: str) -> Any:
    """Extract a JSON object or array from model text.

    The assistant is prompted to return plain JSON, but this helper is
    defensive against fenced code blocks or stray prose.
    """
    stripped = text.strip()
    if not stripped:
        raise ValueError("assistant response was empty")

    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    starts = [idx for idx, ch in enumerate(stripped) if ch in "[{"]
    for start in starts:
        candidate = stripped[start:].strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    ends = [idx for idx, ch in enumerate(stripped) if ch in "]}"]
    for start in starts:
        for end in reversed(ends):
            if end <= start:
                continue
            candidate = stripped[start : end + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue

    raise ValueError("could not extract JSON from assistant response")


def build_qoder_command(
    *,
    prompt: str,
    workspace: Path,
    output_format: str = "json",
    quiet: bool = True,
    yolo: bool = False,
    allowed_tools: Optional[List[str]] = None,
    disallowed_tools: Optional[List[str]] = None,
    max_turns: Optional[int] = None,
    extra_args: Optional[List[str]] = None,
) -> List[str]:
    """Build a headless qodercli command."""
    cmd = [
        "qodercli",
        "-w",
        str(workspace),
        "-p",
        prompt,
        f"--output-format={output_format}",
    ]
    if quiet:
        cmd.append("-q")
    if yolo:
        cmd.append("--yolo")
    if max_turns is not None:
        cmd.extend(["--max-turns", str(max_turns)])
    if allowed_tools:
        cmd.extend(["--allowed-tools", ",".join(allowed_tools)])
    if disallowed_tools:
        cmd.extend(["--disallowed-tools", ",".join(disallowed_tools)])
    if extra_args:
        cmd.extend(extra_args)
    return cmd


def invoke_qoder(
    *,
    prompt: str,
    workspace: Path,
    yolo: bool = False,
    allowed_tools: Optional[List[str]] = None,
    disallowed_tools: Optional[List[str]] = None,
    max_turns: Optional[int] = None,
    extra_args: Optional[List[str]] = None,
    timeout: int = 1800,
) -> Dict[str, Any]:
    """Invoke qodercli headlessly and parse its structured output."""
    cmd = build_qoder_command(
        prompt=prompt,
        workspace=workspace,
        yolo=yolo,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        max_turns=max_turns,
        extra_args=extra_args,
    )
    try:
        proc = run_process(cmd, cwd=workspace, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "command": cmd,
            "exit_code": 124,
            "events": [],
            "text": "",
            "stderr": f"qodercli timed out after {timeout}s",
            "error": "timeout",
        }
    except OSError as exc:
        return {
            "ok": False,
            "command": cmd,
            "exit_code": 127,
            "events": [],
            "text": "",
            "stderr": str(exc),
            "error": "os_error",
        }

    events = parse_json_events(proc.stdout)
    text = event_text(events)
    error_event = next(
        (event for event in events if event.get("type") == "error"),
        None,
    )

    payload: Dict[str, Any] = {
        "ok": proc.returncode == 0 and error_event is None and bool(events),
        "command": cmd,
        "exit_code": proc.returncode,
        "events": events,
        "text": text,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "session_id": (
            events[-1].get("session_id")
            if events and isinstance(events[-1], dict)
            else None
        ),
    }
    if error_event is not None:
        payload["error"] = error_event.get("subtype") or error_event.get("type")
        payload["error_code"] = error_event.get("error_code")
    elif not events:
        payload["error"] = "invalid_json_output"
    return payload


def invoke_qoder_json(
    *,
    prompt: str,
    workspace: Path,
    yolo: bool = False,
    allowed_tools: Optional[List[str]] = None,
    disallowed_tools: Optional[List[str]] = None,
    max_turns: Optional[int] = None,
    extra_args: Optional[List[str]] = None,
    timeout: int = 1800,
) -> Dict[str, Any]:
    """Invoke qodercli and parse a JSON object from the assistant text."""
    result = invoke_qoder(
        prompt=prompt,
        workspace=workspace,
        yolo=yolo,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        max_turns=max_turns,
        extra_args=extra_args,
        timeout=timeout,
    )
    if not result["ok"]:
        return result
    try:
        result["parsed"] = extract_json_value(result["text"])
    except ValueError as exc:
        result["ok"] = False
        result["error"] = "invalid_assistant_json"
        result["stderr"] = (result.get("stderr") or "") + f"\n{exc}"
    return result


def probe_qodercli(workspace: Path) -> Dict[str, Any]:
    """Run the required safe capability probes."""
    help_cmd = ["qodercli", "--help"]
    version_cmd = ["qodercli", "--version"]
    headless_cmd = [
        "qodercli",
        "-w",
        str(workspace),
        "-p",
        "say hello",
        "--output-format=json",
    ]

    def run_probe(cmd: List[str]) -> Dict[str, Any]:
        try:
            proc = run_process(cmd, cwd=workspace, timeout=60)
        except subprocess.TimeoutExpired:
            return {"ok": False, "command": cmd, "exit_code": 124, "error": "timeout"}
        except OSError as exc:
            return {
                "ok": False,
                "command": cmd,
                "exit_code": 127,
                "error": str(exc),
            }
        return {
            "ok": proc.returncode == 0,
            "command": cmd,
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }

    try:
        proc = run_process(headless_cmd, cwd=workspace, timeout=120)
        events = parse_json_events(proc.stdout)
        hello: Dict[str, Any] = {
            "ok": proc.returncode == 0 and bool(events),
            "command": headless_cmd,
            "exit_code": proc.returncode,
            "events": events,
            "text": event_text(events),
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "session_id": (
                events[-1].get("session_id")
                if events and isinstance(events[-1], dict)
                else None
            ),
        }
        error_event = next(
            (event for event in events if event.get("type") == "error"),
            None,
        )
        if error_event is not None:
            hello["ok"] = False
            hello["error"] = error_event.get("subtype") or error_event.get("type")
            hello["error_code"] = error_event.get("error_code")
        elif not events:
            hello["ok"] = False
            hello["error"] = "invalid_json_output"
    except subprocess.TimeoutExpired:
        hello = {
            "ok": False,
            "command": headless_cmd,
            "exit_code": 124,
            "events": [],
            "text": "",
            "stderr": "qodercli timed out after 120s",
            "error": "timeout",
        }
    except OSError as exc:
        hello = {
            "ok": False,
            "command": headless_cmd,
            "exit_code": 127,
            "events": [],
            "text": "",
            "stderr": str(exc),
            "error": "os_error",
        }

    return {
        "binary_found": command_exists("qodercli"),
        "help": run_probe(help_cmd),
        "version": run_probe(version_cmd),
        "headless_probe": hello,
    }


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Invoke qodercli headlessly.")
    parser.add_argument("--probe", action="store_true", help="run safe capability probes")
    parser.add_argument("--workspace", default=".", help="workspace path")
    parser.add_argument("--prompt", default=None, help="prompt to execute")
    parser.add_argument("--json-payload", action="store_true", help="parse assistant text as JSON")
    parser.add_argument("--yolo", action="store_true", help="bypass qodercli permission prompts")
    parser.add_argument("--max-turns", type=int, default=None)
    parser.add_argument("--allowed-tools", nargs="*", default=None)
    parser.add_argument("--disallowed-tools", nargs="*", default=None)
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    if args.probe:
        print(json.dumps(probe_qodercli(workspace), indent=2))
        return 0

    if not args.prompt:
        parser.error("--prompt is required unless --probe is used")

    if args.json_payload:
        result = invoke_qoder_json(
            prompt=args.prompt,
            workspace=workspace,
            yolo=args.yolo,
            allowed_tools=args.allowed_tools,
            disallowed_tools=args.disallowed_tools,
            max_turns=args.max_turns,
            timeout=args.timeout,
        )
    else:
        result = invoke_qoder(
            prompt=args.prompt,
            workspace=workspace,
            yolo=args.yolo,
            allowed_tools=args.allowed_tools,
            disallowed_tools=args.disallowed_tools,
            max_turns=args.max_turns,
            timeout=args.timeout,
        )

    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
