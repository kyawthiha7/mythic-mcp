---
marp: true
theme: default
paginate: true
style: |
  section {
    font-family: 'Segoe UI', sans-serif;
    background: #0d1117;
    color: #e6edf3;
    font-size: 1em;
  }
  h1 { color: #58a6ff; }
  h2 { color: #58a6ff; border-bottom: 1px solid #30363d; padding-bottom: 8px; }
  code { background: #161b22; color: #79c0ff; padding: 2px 6px; border-radius: 4px; }
  pre { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }
  pre code { background: none; padding: 0; color: #e6edf3; }
  table { border-collapse: collapse; width: 100%; }
  th { background: #161b22; color: #58a6ff; border: 1px solid #30363d; padding: 8px 12px; }
  td { border: 1px solid #30363d; padding: 8px 12px; font-size: 0.9em; }
  .highlight { color: #ffa657; font-weight: bold; }
---

# mythic-mcp
## Mythic C2 × Model Context Protocol

Control your C2 framework through natural language

---

## What is This Project?

`mythic-mcp` is a **bridge** between Claude and the Mythic C2 framework.

- Wraps the Mythic GraphQL/REST API as **MCP tools**
- Claude calls these tools like function calls
- Operator describes intent in plain English; Claude handles the API calls

**Two files. No extra infra.**

```
mythic-mcp/
├── main.py          # MCP server — tool definitions exposed to Claude
└── lib/
    └── mythic_api.py  # MythicAPI class — all Mythic communication
```

Dependencies: `mcp[cli]`, `mythic` (official Python library), `httpx`

---

## Layer 1 — MCP Server (`main.py`)

Uses **FastMCP** — a decorator-based framework for defining MCP tools.

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("mythic_mcp")   # server name Claude sees
api: MythicAPI | None = None  # single shared API instance
```

Every tool is just a Python `async def` with a `@mcp.tool()` decorator.
FastMCP reads the **docstring + type hints** to generate the tool schema
that Claude receives — no manual JSON schema needed.

```python
@mcp.tool()
async def set_sleep(agent_id: int, interval: int, jitter: int) -> str:
    """Change the sleep interval and jitter on a running agent.

    Args:
        agent_id: Display ID of the agent.
        interval: New sleep interval in seconds.
        jitter: Jitter percentage (0-100).
    """
    output = await api.set_sleep(agent_id, interval, jitter)
    return _ok({"output": output})
```

---

## Layer 1 — Structured Responses

Every tool returns a **consistent JSON envelope** so Claude can reliably
detect success vs. failure without parsing free-form text.

```python
def _ok(data: Any) -> str:
    return json.dumps({"status": "ok", "data": data}, default=str)

def _err(msg: str) -> str:
    return json.dumps({"status": "error", "message": msg})
```

Every tool wraps its call in `try/except MythicAPIError`:

```python
@mcp.tool()
async def get_all_agents() -> str:
    try:
        agents = await api.get_all_agents()
        return _ok(agents)          # {"status": "ok", "data": [...]}
    except MythicAPIError as e:
        return _err(str(e))         # {"status": "error", "message": "..."}
```

Claude can branch on `status` without guessing at output format.

---

## Layer 2 — MythicAPI (`lib/mythic_api.py`)

Handles all communication with Mythic. Two transport mechanisms:

**1 — Official Mythic Python library** (agent tasks, payload builds)
```python
from mythic import mythic, mythic_classes

self.mythic_instance = await mythic.login(
    username=..., password=...,
    server_ip=..., server_port=...
)
```

**2 — Raw GraphQL over `httpx`** (queries that the library doesn't expose)
```python
async def _graphql(self, query: str, variables: dict | None = None) -> dict:
    url = f"{self._base_url()}/graphql/"
    async with self._http_client() as client:
        resp = await client.post(url, json={"query": query, "variables": variables},
                                 headers=self._headers())
        resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise MythicAPIError(f"GraphQL error: {data['errors']}")
    return data.get("data", {})
```

`httpx.AsyncClient(verify=False)` — Mythic uses self-signed TLS certs.

---

## Layer 2 — `_issue_task`: The Core Helper

Almost every agent operation goes through one shared helper:

```python
async def _issue_task(self, agent_id: int,
                      command_name: str, parameters) -> str:
    output = await asyncio.wait_for(
        mythic.issue_task_and_waitfor_task_output(
            self.mythic_instance,
            command_name=command_name,
            parameters=parameters,
            callback_display_id=agent_id,
        ),
        timeout=TASK_TIMEOUT,   # default 60s
    )
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return str(output)
```

- Issues the task **and blocks** until output comes back
- `asyncio.wait_for` enforces the 60s timeout — raises `MythicAPIError` on expiry
- Returns raw string output — callers wrap it in `_ok()`

Used by: `shell`, `cat`, `make_token`, `mimikatz`, `sleep`, and `execute_command`.

---

## Layer 2 — GraphQL Example: `get_all_agents`

Queries the `callback` table directly for active callbacks with full detail.

```python
_CALLBACK_FIELDS = """
    id  display_id  agent_callback_id
    host  user  os  architecture  pid
    ip  external_ip  integrity_level  domain
    sleep_info  last_checkin  process_name
    payload { uuid  payloadtype { name } }
"""

async def get_all_agents(self) -> list[dict]:
    data = await self._graphql(f"""
        query GetActiveCallbacks {{
            callback(
                where: {{active: {{_eq: true}}}},
                order_by: {{display_id: asc}}
            ) {{
                {self._CALLBACK_FIELDS}
            }}
        }}
    """)
    return data.get("callback", [])
```

The `display_id` is what operators see in the Mythic UI — used as `agent_id`
in all tool calls so operators don't need to look up internal UUIDs.

---

## Putting It Together — Payload Creation Flow

`create_payload` delegates entirely to the Mythic Python library:

```python
async def create_payload(self, payload_type, operating_system,
                         filename, c2_profile, c2_params, ...) -> dict:
    return await mythic_mod.create_payload(
        mythic=self.mythic_instance,
        payload_type_name=payload_type,     # e.g. "apollo"
        filename=filename,                   # e.g. "mythic.exe"
        operating_system=operating_system,   # e.g. "Windows"
        c2_profiles=[{
            "c2_profile": c2_profile,        # e.g. "http"
            "c2_profile_parameters": c2_params  # {"callback_host": ..., ...}
        }],
        return_on_complete=True,
        timeout=timeout,
        include_all_commands=True,
    )
```

Then `save_payload_locally` downloads the built binary:

```python
data = await mythic_mod.download_payload(mythic=self.mythic_instance,
                                          payload_uuid=payload_uuid)
with open(local_path, "wb") as fh:
    fh.write(data)
```

