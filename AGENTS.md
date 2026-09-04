# Repository guidance

This repository is the GitHub Copilot and VS Code adapter for AAE. Keep portable
workflow, skill registry, and event semantics in `adaptive-agentic-engineering`.
Keep only native VS Code and GitHub Copilot configuration, payload translation,
capability verification, and adapter tests here.

Prefer current native product surfaces. Verify upstream documentation before
changing their schemas. Do not add compatibility abstractions without a real
second use case. Never overwrite existing project customization files or
hand-authored native skills.
