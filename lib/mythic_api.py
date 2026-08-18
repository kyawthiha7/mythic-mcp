import asyncio
import json
import ssl
import httpx
from mythic import mythic, mythic_classes


TASK_TIMEOUT = 60  # seconds to wait for a task to complete


class MythicAPIError(Exception):
    pass


class MythicAPI:
    def __init__(self, username: str, password: str, server_ip: str, server_port: str, use_ssl: bool = True):
        self.username = username
        self.password = password
        self.server_ip = server_ip
        self.server_port = server_port
        self.use_ssl = use_ssl
        self.mythic_instance = None

    async def connect(self):
        self.mythic_instance = await mythic.login(
            username=self.username,
            password=self.password,
            server_ip=self.server_ip,
            server_port=self.server_port,
        )

    # ---------------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------------

    def _base_url(self) -> str:
        scheme = "https" if self.use_ssl else "http"
        return f"{scheme}://{self.server_ip}:{self.server_port}"

    def _headers(self) -> dict:
        token = getattr(self.mythic_instance, "access_token", None)
        if not token:
            raise MythicAPIError("Not connected or missing access token")
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _http_client(self) -> httpx.AsyncClient:
        # Mythic typically uses self-signed certs; skip verification by default.
        return httpx.AsyncClient(verify=False, timeout=30)

    async def _graphql(self, query: str, variables: dict | None = None) -> dict:
        url = f"{self._base_url()}/graphql/"
        payload: dict = {"query": query}
        if variables:
            payload["variables"] = variables
        async with self._http_client() as client:
            resp = await client.post(url, json=payload, headers=self._headers())
            resp.raise_for_status()
            data = resp.json()
        if "errors" in data:
            raise MythicAPIError(f"GraphQL error: {data['errors']}")
        return data.get("data", {})

    async def _issue_task(self, agent_id: int, command_name: str, parameters) -> str:
        try:
            output = await asyncio.wait_for(
                mythic.issue_task_and_waitfor_task_output(
                    self.mythic_instance,
                    command_name=command_name,
                    parameters=parameters,
                    callback_display_id=agent_id,
                ),
                timeout=TASK_TIMEOUT,
            )
            if isinstance(output, bytes):
                return output.decode(errors="replace")
            return str(output)
        except asyncio.TimeoutError:
            raise MythicAPIError(f"Task '{command_name}' timed out after {TASK_TIMEOUT}s")
        except Exception as e:
            raise MythicAPIError(f"Task '{command_name}' failed: {e}")

    # ---------------------------------------------------------------------------
    # Feature 1 — expose download_file (was implemented but never exposed)
    # ---------------------------------------------------------------------------

    async def download_file(self, agent_id: int, file_path: str) -> bytes:
        try:
            task = await asyncio.wait_for(
                mythic.issue_task(
                    mythic=self.mythic_instance,
                    command_name="download",
                    parameters={"path": file_path},
                    wait_for_complete=True,
                    callback_display_id=agent_id,
                ),
                timeout=TASK_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise MythicAPIError(f"download task timed out after {TASK_TIMEOUT}s")
        except Exception as e:
            raise MythicAPIError(f"download task failed: {e}")

        # Retrieve the file content from Mythic's file store.
        task_id = task.get("id") if isinstance(task, dict) else None
        if task_id is None:
            raise MythicAPIError("download task did not return a task id")

        data = await self._graphql(
            """
            query GetDownloadedFile($task_id: Int!) {
                filemeta(where: {task_id: {_eq: $task_id}}) {
                    agent_file_id
                    filename_text
                    size
                }
            }
            """,
            {"task_id": task_id},
        )
        files = data.get("filemeta", [])
        if not files:
            raise MythicAPIError("No file found in Mythic for that download task")
        file_id = files[0]["agent_file_id"]

        url = f"{self._base_url()}/direct/download/{file_id}"
        async with self._http_client() as client:
            resp = await client.get(url, headers=self._headers())
            resp.raise_for_status()
            return resp.content

    # ---------------------------------------------------------------------------
    # Feature 2 — richer callback management
    # ---------------------------------------------------------------------------

    _CALLBACK_FIELDS = """
        id
        display_id
        agent_callback_id
        host
        user
        description
        os
        architecture
        pid
        ip
        external_ip
        integrity_level
        domain
        sleep_info
        last_checkin
        init_callback
        process_name
        payload {
            uuid
            payloadtype {
                name
            }
        }
    """

    async def get_all_agents(self) -> list[dict]:
        data = await self._graphql(
            f"""
            query GetActiveCallbacks {{
                callback(where: {{active: {{_eq: true}}}}, order_by: {{display_id: asc}}) {{
                    {self._CALLBACK_FIELDS}
                }}
            }}
            """
        )
        return data.get("callback", [])

    async def get_agent_details(self, agent_id: int) -> dict | None:
        data = await self._graphql(
            f"""
            query GetCallback($display_id: Int!) {{
                callback(where: {{display_id: {{_eq: $display_id}}, active: {{_eq: true}}}}) {{
                    {self._CALLBACK_FIELDS}
                }}
            }}
            """,
            {"display_id": agent_id},
        )
        callbacks = data.get("callback", [])
        return callbacks[0] if callbacks else None

    async def kill_agent(self, agent_id: int) -> bool:
        data = await self._graphql(
            """
            mutation KillCallback($display_id: Int!) {
                update_callback(
                    where: {display_id: {_eq: $display_id}},
                    _set: {active: false}
                ) {
                    affected_rows
                }
            }
            """,
            {"display_id": agent_id},
        )
        rows = data.get("update_callback", {}).get("affected_rows", 0)
        return rows > 0

    async def set_sleep(self, agent_id: int, interval: int, jitter: int) -> str:
        # Issues the standard "sleep" command understood by most Mythic agents
        # (Apollo, Poseidon, Medusa, Athena all accept this form).
        return await self._issue_task(
            agent_id,
            "sleep",
            {"interval": interval, "jitter": jitter},
        )

    # ---------------------------------------------------------------------------
    # Original tools — fixed (feature 11)
    # ---------------------------------------------------------------------------

    async def execute_shell_command(self, agent_id: int, command: str) -> str:
        return await self._issue_task(agent_id, "shell", command)

    async def read_file(self, agent_id: int, file_path: str) -> str:
        return await self._issue_task(agent_id, "cat", {"path": file_path})

    async def make_token(self, agent_id: int, username: str, password: str) -> str:
        return await self._issue_task(
            agent_id,
            "make_token",
            {"username": username, "password": password},
        )

    async def execute_mimikatz(self, agent_id: int, mimikatz_command: str) -> str:
        return await self._issue_task(agent_id, "mimikatz", {"commands": mimikatz_command})

    async def upload_file(self, agent_id: int, filename: str, file_path: str, contents: bytes) -> bool:
        try:
            file_id = await mythic.register_file(
                mythic=self.mythic_instance, filename=filename, contents=contents
            )
            status = await asyncio.wait_for(
                mythic.issue_task(
                    mythic=self.mythic_instance,
                    command_name="upload",
                    parameters={"remote_path": file_path, "file": file_id},
                    callback_display_id=agent_id,
                    wait_for_complete=True,
                ),
                timeout=TASK_TIMEOUT,
            )
            return isinstance(status, dict) and status.get("status") == "success"
        except asyncio.TimeoutError:
            raise MythicAPIError(f"upload task timed out after {TASK_TIMEOUT}s")
        except Exception as e:
            raise MythicAPIError(f"upload task failed: {e}")

    async def execute_command(self, agent_id: int, command_name: str, parameters) -> str:
        """Generic task dispatcher — not limited to hardcoded command names."""
        return await self._issue_task(agent_id, command_name, parameters)

    # ---------------------------------------------------------------------------
    # Feature 10 — Mythic platform features
    # ---------------------------------------------------------------------------

    async def get_credentials(self) -> list[dict]:
        data = await self._graphql(
            """
            query GetCredentials {
                credential(order_by: {id: asc}) {
                    id
                    credential_type
                    account
                    credential
                    realm
                    comment
                    timestamp
                }
            }
            """
        )
        return data.get("credential", [])

    async def get_artifacts(self, limit: int = 200) -> list[dict]:
        data = await self._graphql(
            """
            query GetArtifacts($limit: Int!) {
                taskartifact(order_by: {id: desc}, limit: $limit) {
                    id
                    artifact
                    artifact_type
                    timestamp
                    host
                    task {
                        display_id
                        command_name
                        callback {
                            display_id
                            host
                        }
                    }
                }
            }
            """,
            {"limit": limit},
        )
        return data.get("taskartifact", [])

    async def get_event_log(self, limit: int = 100) -> list[dict]:
        data = await self._graphql(
            """
            query GetEventLog($limit: Int!) {
                operationeventlog(order_by: {id: desc}, limit: $limit) {
                    id
                    level
                    message
                    timestamp
                    operator {
                        username
                    }
                }
            }
            """,
            {"limit": limit},
        )
        return data.get("operationeventlog", [])

    async def browse_files(self, agent_id: int, path: str = "/") -> list[dict]:
        # mythictree holds file browser data keyed by callback id (not display id).
        # First resolve display_id -> internal callback id.
        cb_data = await self._graphql(
            """
            query ResolveCallbackId($display_id: Int!) {
                callback(where: {display_id: {_eq: $display_id}}) {
                    id
                }
            }
            """,
            {"display_id": agent_id},
        )
        callbacks = cb_data.get("callback", [])
        if not callbacks:
            raise MythicAPIError(f"No active callback with display_id {agent_id}")
        internal_id = callbacks[0]["id"]

        data = await self._graphql(
            """
            query BrowseFiles($callback_id: Int!, $path: String!) {
                mythictree(
                    where: {
                        callback_id: {_eq: $callback_id},
                        parent_path_text: {_eq: $path}
                    },
                    order_by: {name_text: asc}
                ) {
                    id
                    name_text
                    full_path_text
                    is_file
                    size
                    modify_time
                    success
                }
            }
            """,
            {"callback_id": internal_id, "path": path},
        )
        return data.get("mythictree", [])

    async def list_payloads(self) -> list[dict]:
        data = await self._graphql(
            """
            query ListPayloads {
                payload(where: {deleted: {_eq: false}}, order_by: {id: asc}) {
                    id
                    uuid
                    description
                    build_phase
                    timestamp
                    payloadtype {
                        name
                    }
                    c2profileparametersinstances {
                        c2profile {
                            name
                        }
                    }
                }
            }
            """
        )
        return data.get("payload", [])

    async def list_c2_profiles(self) -> list[dict]:
        data = await self._graphql(
            """
            query ListC2Profiles {
                c2profile(order_by: {id: asc}) {
                    id
                    name
                    description
                    is_p2p
                    is_exit
                    running
                }
            }
            """
        )
        return data.get("c2profile", [])

    async def start_c2_listener(self, c2_profile_name: str, action: str = "start") -> dict:
        from mythic import mythic as mythic_mod
        return await mythic_mod.start_stop_c2_profile(
            mythic=self.mythic_instance,
            c2_profile_name=c2_profile_name,
            action=action,
        )

    async def create_payload(
        self,
        payload_type: str,
        operating_system: str,
        filename: str,
        c2_profile: str,
        c2_params: dict,
        description: str = "",
        timeout: int = 120,
    ) -> dict:
        from mythic import mythic as mythic_mod
        c2_profiles = [{"c2_profile": c2_profile, "c2_profile_parameters": c2_params}]
        return await mythic_mod.create_payload(
            mythic=self.mythic_instance,
            payload_type_name=payload_type,
            filename=filename,
            operating_system=operating_system,
            c2_profiles=c2_profiles,
            description=description,
            return_on_complete=True,
            timeout=timeout,
            include_all_commands=True,
        )

    async def download_payload_bytes(self, payload_uuid: str) -> bytes:
        from mythic import mythic as mythic_mod
        return await mythic_mod.download_payload(
            mythic=self.mythic_instance,
            payload_uuid=payload_uuid,
        )
