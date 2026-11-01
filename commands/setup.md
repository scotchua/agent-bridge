---
description: Set up the agent-bridge Claude to Codex plugin
---

Run the plugin's packaged setup entry point. It only creates the Claude to
Codex connection. The plugin supports macOS, Linux, and WSL. On native Windows,
stop and tell the user to use the clone install in INSTALL.md instead.

Before the live synthetic verification, explain that it uses the user's Codex
CLI subscription and ask for confirmation. If Codex needs a browser login, ask
the user to complete the browser flow themselves. Never ask for a password,
token, API key, or OAuth code. Do not install Python or either CLI.

After confirmation, run `${CLAUDE_PLUGIN_ROOT}/bin/agent-bridge-plugin-setup`
as an argument-array command.
