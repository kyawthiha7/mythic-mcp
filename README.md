# mythic-mcp

Exposes the [Mythic C2](https://github.com/its-a-feature/Mythic) framework as an [MCP](https://modelcontextprotocol.io/) server so LLMs (Claude, etc.) can operate agents, issue tasks, and query platform data conversationally.

## Features

- **Agent management** — list active callbacks with full detail, inspect individual agents, kill agents, change sleep timers
- **Task execution** — shell commands, file read/download/upload, mimikatz, make_token, and a generic dispatcher for any Mythic command
- **Platform data** — credentials store, artifact log, event log, file browser, payload list, C2 profile status
- **Structured responses** — every tool returns `{"status":"ok","data":...}` or `{"status":"error","message":...}` for reliable LLM parsing
- **Built-in prompts** — `start_pentest` and `start_recon` prompt templates

## Requirements

- Python 3.10+
- [uv](https://github.com/astral-sh/uv) package manager
- A running Mythic server (v3.x+)

## Installation

```bash
git clone <this-repo>
cd mythic-mcp
uv sync
```

## Running

### Positional arguments

```bash
uv run main.py <username> <password> [host] [port]
```

```bash
uv run main.py mythic_admin mythic_admin_password localhost 7443
```

### Environment variables

```bash
export MYTHIC_USERNAME=mythic_admin
export MYTHIC_PASSWORD=mythic_admin_password
export MYTHIC_HOST=localhost
export MYTHIC_PORT=7443
uv run main.py
```

### Without TLS

```bash
uv run main.py mythic_admin password localhost 7443 --no-ssl
```

If the server starts silently with no output, it is working — it waits on stdio for MCP messages.

## Connecting to Claude Desktop

Find the config file:

| OS    | Path                                                         |
|-------|--------------------------------------------------------------|
| Linux | `~/.config/Claude/claude_desktop_config.json`               |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |

Get the `uv` path first:

```bash
which uv
```

Add an entry under `mcpServers`:

```json
{
  "mcpServers": {
    "mythic_mcp": {
      "command": "/home/user/.local/bin/uv",
      "args": [
        "--directory",
        "/path/to/mythic-mcp",
        "run",
        "main.py"
      ],
      "env": {
        "MYTHIC_USERNAME": "mythic_admin",
        "MYTHIC_PASSWORD": "mythic_admin_password",
        "MYTHIC_HOST": "localhost",
        "MYTHIC_PORT": "7443"
      }
    }
  }
}
```

Fully quit and relaunch Claude Desktop. The tools icon in the chat UI will list all registered tools once the server connects.

## Tools

### Agent / Callback management

| Tool | Description |
|------|-------------|
| `get_all_agents()` | List all active callbacks with full detail (OS, arch, PID, IP, sleep, process, payload type, etc.) |
| `get_agent_details(agent_id)` | Full detail for a single agent by display ID |
| `kill_agent(agent_id)` | Mark an agent inactive in Mythic |
| `set_sleep(agent_id, interval, jitter)` | Change sleep interval (seconds) and jitter (%) |

### Task execution

| Tool | Description |
|------|-------------|
| `run_shell_command(agent_id, command_line)` | Run a shell command via the agent's default interpreter |
| `read_file(agent_id, file_path)` | Read a file from the target (uses `cat`) |
| `download_file(agent_id, file_path)` | Download a file and return it as base64 |
| `upload_file(agent_id, file_name, remote_path, content_b64)` | Upload a base64-encoded file to the target |
| `run_as_user(agent_id, username, password)` | `make_token` — authenticate for network calls as another user |
| `execute_mimikatz(agent_id, mimikatz_arguments)` | Run mimikatz (e.g. `sekurlsa::logonpasswords`) |
| `execute_command(agent_id, command_name, parameters)` | Generic dispatcher — any Mythic command by name |

### Platform data

| Tool | Description |
|------|-------------|
| `get_credentials()` | All credentials in the Mythic credential store |
| `get_artifacts(limit)` | Artifact log (files, registry keys, processes created) |
| `get_event_log(limit)` | Operation event log |
| `browse_files(agent_id, path)` | File browser entries for a directory on a target |
| `list_payloads()` | All non-deleted payloads with build status and C2 profile |
| `list_c2_profiles()` | All C2 profiles with running status |

### Prompts

| Prompt | Description |
|--------|-------------|
| `start_pentest(threat_actor, objective)` | Prime Claude to emulate a specific threat actor toward an objective |
| `start_recon()` | Prime Claude to perform initial recon across all active agents |

## Example usage in Claude

```
Use the start_recon prompt.
```

```
List all active agents, then run whoami on agent 1.
```

```
Download /etc/passwd from agent 2 and show me its contents.
```

```
Run mimikatz sekurlsa::logonpasswords on agent 3.
```

```
Use execute_command to run 'ps' on agent 1 with parameters {"host": "."}.
```

## Architecture

```
Claude Desktop
     │  MCP stdio
     ▼
main.py  (FastMCP tools + prompts)
     │
     ▼
lib/mythic_api.py  (MythicAPI class)
     ├─ mythic Python library  →  Mythic GraphQL/REST API
     └─ httpx (raw GraphQL)   →  Mythic GraphQL endpoint
```

All task calls use `asyncio.wait_for` with a 60-second timeout. GraphQL requests skip TLS verification (`verify=False`) to handle Mythic's self-signed certificate.

## Environment variables reference

| Variable           | Default     | Description          |
|--------------------|-------------|----------------------|
| `MYTHIC_USERNAME`  | —           | Mythic operator username (required) |
| `MYTHIC_PASSWORD`  | —           | Mythic operator password (required) |
| `MYTHIC_HOST`      | `localhost` | Mythic server hostname or IP |
| `MYTHIC_PORT`      | `7443`      | Mythic server port |
