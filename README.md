# AAE GitHub Copilot Adapter

Native GitHub Copilot integration for
[Adaptive Agentic Engineering](https://github.com/Wolfeitz/adaptive-agentic-engineering),
targeting VS Code's current customization surfaces.

The adapter uses `.github/copilot-instructions.md`, path-specific instructions,
agent skills, prompt files, custom agents, and preview hooks. It does not
recreate those mechanisms inside AAE core.

```bash
python -m pip install -e ../adaptive-agentic-engineering -e .
aae-github-copilot init /path/to/project
aae-github-copilot doctor /path/to/project
```

`init` preserves existing files. `sync-skills` updates only adapter-generated
projections under `.github/skills`; it never replaces a hand-authored native
skill. The capability manifest links the upstream documentation used for the
current implementation, and scheduled verification makes documentation drift
visible.

VS Code hooks remain a preview feature as of the manifest's verification date.
Projects can omit the hook template while continuing to use all stable native
surfaces.
