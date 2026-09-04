from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

from aae_github_copilot.cli import PROJECTION_MARKER, handle_hook, install, sync_skills


class GitHubCopilotAdapterTests(unittest.TestCase):
    def test_init_installs_native_files_and_preserves_existing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            instructions = root / ".github/copilot-instructions.md"
            instructions.parent.mkdir(parents=True)
            instructions.write_text("existing", encoding="utf-8")
            self.assertEqual(install(root), 0)
            self.assertEqual(instructions.read_text(), "existing")
            self.assertTrue((root / ".github/hooks/aae.json").is_file())
            self.assertTrue((root / ".github/agents/aae-independent-reviewer.agent.md").is_file())

    def test_projects_skills_without_replacing_native_authorship(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / ".aae/skills/repo-recon"
            source.mkdir(parents=True)
            (source / "skill.json").write_text(
                json.dumps({"name": "repo-recon", "description": "Inspect the repository"}),
                encoding="utf-8",
            )
            (source / "SKILL.md").write_text("# Procedure\n", encoding="utf-8")
            self.assertEqual(sync_skills(root), [])
            projected = root / ".github/skills/repo-recon/SKILL.md"
            self.assertIn(PROJECTION_MARKER, projected.read_text())
            projected.write_text("hand-authored", encoding="utf-8")
            self.assertTrue(sync_skills(root))
            self.assertEqual(projected.read_text(), "hand-authored")

    def test_hook_sanitizes_payload_and_extracts_camel_case_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            aae = root / ".aae"
            aae.mkdir()
            (aae / "hooks.json").write_text(json.dumps({
                "schema_version": 1,
                "rules": [{
                    "id": "check-python",
                    "on": "files-changed",
                    "paths": ["src/**/*.py"],
                    "run_check": [sys.executable, "-c", "raise SystemExit(0)"],
                }],
            }), encoding="utf-8")
            secret = "never-persist-this-response"
            output = handle_hook(root, {
                "hook_event_name": "PostToolUse",
                "session_id": "session-1",
                "tool_name": "replace_string_in_file",
                "tool_input": {"filePath": "src/aae/core.py"},
                "tool_response": secret,
            })
            self.assertIsNone(output)
            record = next((root / ".aae/runtime/hook-events").glob("*.json")).read_text()
            self.assertNotIn(secret, record)
            self.assertIn('"adapter": "github-copilot"', record)


if __name__ == "__main__":
    unittest.main()
