# my-plugin

Starter Claude Code plugin.

## Try it locally

    claude --plugin-dir ./my-plugin

Then run `/my-plugin:hello`.

## Layout

- `.claude-plugin/plugin.json` – manifest (only file in `.claude-plugin/`)
- `commands/` – slash commands (markdown)
- `skills/<name>/SKILL.md` – skills Claude invokes automatically
- Optional: `agents/`, `hooks/hooks.json`, `.mcp.json`
