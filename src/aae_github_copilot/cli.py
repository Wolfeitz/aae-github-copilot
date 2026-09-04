from __future__ import annotations

import argparse
from datetime import date
import hashlib
import importlib.resources
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Iterable, cast
import urllib.error
import urllib.request

from aae.hooks import find_aae_root, process_event


MAX_PAYLOAD_BYTES = 1_048_576
MAX_CONTEXT_CHARS = 8_000
EDIT_TOOLS = {
    "create_file",
    "replace_string_in_file",
    "multi_replace_string_in_file",
    "editFiles",
    "write_file",
}
PROJECTION_MARKER = "<!-- aae-adapter-projection: github-copilot"
UPSTREAM_MARKERS = {
    "custom-instructions": ".github/copilot-instructions.md",
    "agent-skills": ".github/skills",
    "hooks": ".github/hooks",
    "custom-agents": ".github/agents",
    "prompt-files": ".github/prompts",
    "mcp-servers": ".vscode/mcp.json",
}


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _portable_path(root: Path, value: str) -> str | None:
    if not value or len(value) > 4096 or any(char in value for char in "\n\r\x00"):
        return None
    candidate = Path(value.replace("\\", "/"))
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve().relative_to(root.resolve())
        except ValueError:
            return None
    portable = candidate.as_posix().removeprefix("./")
    return None if not portable or ".." in Path(portable).parts else portable


def _changed_paths(root: Path, tool_input: object) -> list[str]:
    if not isinstance(tool_input, dict):
        return []
    values: list[str] = []
    for key in ("filePath", "file_path", "path"):
        value = tool_input.get(key)
        if isinstance(value, str):
            values.append(value)
    for key in ("files", "edits", "replacements"):
        entries = tool_input.get(key, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, str):
                values.append(entry)
            elif isinstance(entry, dict):
                for path_key in ("filePath", "file_path", "path"):
                    value = entry.get(path_key)
                    if isinstance(value, str):
                        values.append(value)
    return sorted({path for value in values if (path := _portable_path(root, value))})


def _template_root() -> Any:
    return importlib.resources.files("aae_github_copilot").joinpath("templates")


def install(root: Path) -> int:
    installed: list[str] = []
    preserved: list[str] = []
    for resource in _template_root().rglob("*"):
        if not resource.is_file() or resource.name.endswith(".pyc"):
            continue
        relative = Path(*resource.relative_to(_template_root()).parts)
        destination = root / relative
        if destination.exists():
            preserved.append(relative.as_posix())
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        with importlib.resources.as_file(resource) as source:
            shutil.copyfile(source, destination)
        installed.append(relative.as_posix())
    sync_errors = sync_skills(root)
    print(json.dumps({"installed": installed, "preserved": preserved, "skill_errors": sync_errors}, indent=2))
    return 1 if sync_errors else 0


def sync_skills(root: Path) -> list[str]:
    errors: list[str] = []
    source_root = root / ".aae/skills"
    if not source_root.is_dir():
        return []
    for manifest_path in sorted(source_root.glob("*/skill.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            name = manifest["name"]
            description = manifest["description"]
            procedure_path = manifest_path.parent / str(manifest.get("procedure", "SKILL.md"))
            procedure = procedure_path.read_text(encoding="utf-8")
            if not isinstance(name, str) or not isinstance(description, str):
                raise ValueError("name and description must be strings")
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, ValueError) as error:
            errors.append(f"{manifest_path}: {error}")
            continue
        source_sha256 = hashlib.sha256(
            manifest_path.read_bytes() + b"\0" + procedure.encode()
        ).hexdigest()
        destination = root / ".github/skills" / name / "SKILL.md"
        if destination.exists() and PROJECTION_MARKER not in destination.read_text(encoding="utf-8"):
            errors.append(f"Preserved non-AAE native skill: {destination}")
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            "---\n"
            f"name: {name}\n"
            f"description: {json.dumps(description.replace(chr(10), ' ').strip())}\n"
            "---\n"
            f"{PROJECTION_MARKER} source-sha256: {source_sha256} -->\n\n"
            + procedure.lstrip(),
            encoding="utf-8",
        )
    return errors


def handle_hook(start: Path, native: dict[str, Any]) -> dict[str, Any] | None:
    root = find_aae_root(start)
    if root is None:
        return None
    event_name = native.get("hook_event_name", "PostToolUse")
    tool_name = native.get("tool_name")
    if event_name != "PostToolUse" or tool_name not in EDIT_TOOLS:
        return None
    paths = _changed_paths(root, native.get("tool_input", {}))
    if not paths:
        return None
    native_sha256 = _digest(native)
    identifiers = {
        key: native[key]
        for key in ("session_id", "timestamp", "tool_use_id")
        if isinstance(native.get(key), str)
    }
    record, procedures, errors = process_event(
        root,
        event="files-changed",
        payload={"paths": paths},
        idempotency_key="github-copilot:" + _digest({"payload": native_sha256, **identifiers}),
        record_no_match=False,
        delivery_provenance={
            "adapter": "github-copilot",
            "native_event": event_name,
            "payload_sha256": native_sha256,
            "tool_name": tool_name,
            "paths": paths,
            **identifiers,
        },
    )
    messages = list(procedures.values())
    if errors:
        messages.append("AAE adapter errors: " + "; ".join(errors))
    failed = record.get("status") in {
        "failed",
        "denied",
        "configuration-invalid",
        "chain-depth-denied",
        "action-budget-denied",
    }
    if failed and not errors:
        messages.append(f"AAE event status: {record.get('status')}")
    if not messages:
        return None
    context = "\n\n".join(messages)[:MAX_CONTEXT_CHARS]
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": context,
        }
    }


def hook_command(path: Path) -> int:
    raw = sys.stdin.read(MAX_PAYLOAD_BYTES + 1)
    if len(raw.encode()) > MAX_PAYLOAD_BYTES:
        print("GitHub Copilot hook payload exceeds 1 MiB", file=sys.stderr)
        return 1
    try:
        native = json.loads(raw)
    except json.JSONDecodeError as error:
        print(f"Invalid GitHub Copilot hook JSON: {error}", file=sys.stderr)
        return 1
    if not isinstance(native, dict):
        print("GitHub Copilot hook payload must be an object", file=sys.stderr)
        return 1
    output = handle_hook(path, native)
    if output is not None:
        print(json.dumps(output, separators=(",", ":")))
    return 0


def _capabilities() -> dict[str, Any]:
    resource = importlib.resources.files("aae_github_copilot").joinpath("capabilities.json")
    return cast(dict[str, Any], json.loads(resource.read_text(encoding="utf-8")))


def doctor(root: Path, strict: bool) -> int:
    manifest = _capabilities()
    executable = shutil.which("code")
    version = None
    if executable:
        result = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, check=False
        )
        version = result.stdout.splitlines()[0] if result.stdout else result.stderr.strip()
    project_files = {
        ".github/copilot-instructions.md": (root / ".github/copilot-instructions.md").is_file(),
        ".github/hooks/aae.json": (root / ".github/hooks/aae.json").is_file(),
        ".github/agents/aae-independent-reviewer.agent.md": (
            root / ".github/agents/aae-independent-reviewer.agent.md"
        ).is_file(),
    }
    age_days = (date.today() - date.fromisoformat(manifest["verified_at"])).days
    print(json.dumps({
        "adapter": "github-copilot",
        "runtime": {"executable": executable, "version": version},
        "verification_age_days": age_days,
        "native_hooks_status": "preview",
        "project_files": project_files,
    }, indent=2))
    healthy = bool(executable and all(project_files.values()))
    return 1 if strict and not healthy else 0


def verify_upstream() -> int:
    failures: list[str] = []
    for url in _capabilities()["sources"]:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "aae-github-copilot-verifier/0.1"})
            with urllib.request.urlopen(request, timeout=30) as response:
                content = response.read(2_000_001)
                marker = next(
                    (value for key, value in UPSTREAM_MARKERS.items() if url.endswith(key)),
                    None,
                )
                if response.status != 200:
                    failures.append(f"{url}: HTTP {response.status}")
                elif len(content) > 2_000_000:
                    failures.append(f"{url}: response exceeds 2 MB")
                elif marker is None or marker.encode() not in content:
                    failures.append(f"{url}: expected marker missing")
        except (OSError, urllib.error.URLError) as error:
            failures.append(f"{url}: {error}")
    print(json.dumps({"checked": _capabilities()["sources"], "failures": failures}, indent=2))
    return 1 if failures else 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="AAE native GitHub Copilot and VS Code adapter")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("init", "sync-skills", "hook"):
        command = commands.add_parser(name)
        command.add_argument("path", nargs="?", default=".")
    doctor_parser = commands.add_parser("doctor")
    doctor_parser.add_argument("path", nargs="?", default=".")
    doctor_parser.add_argument("--strict", action="store_true")
    commands.add_parser("verify-upstream")
    return root


def main(argv: Iterable[str] | None = None) -> int:
    arguments = parser().parse_args(list(argv) if argv is not None else None)
    if arguments.command == "verify-upstream":
        return verify_upstream()
    root = Path(arguments.path).resolve()
    if arguments.command == "init":
        return install(root)
    if arguments.command == "sync-skills":
        errors = sync_skills(root)
        print(json.dumps({"errors": errors}, indent=2))
        return 1 if errors else 0
    if arguments.command == "hook":
        return hook_command(root)
    return doctor(root, arguments.strict)
