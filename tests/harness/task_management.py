"""TaskManagementEnv -- integration test environment for list_tasks.

list_tasks is an MCP-only surface: no A2A raw wrapper (the A2A task
polling handlers ``on_get_task``/``on_list_tasks`` are a separate, native
A2A task-lifecycle concept, not a caller of this module) and no REST route.
``call_a2a``/``call_rest`` are intentionally left unimplemented (base class
default raises ``NotImplementedError``).

Requires: integration_db fixture.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from tests.harness._base import IntegrationEnv


class GetTaskWireResponse(BaseModel):
    """Full success-path wire shape for get_task — every returned field declared.

    ``extra="allow"`` with only 3 of ~12 fields would let a leaked or dropped
    field pass silently on the payload this PR gates access to; this model
    mirrors the full ``task_detail`` dict built in
    ``src.core.tools.task_management.get_task`` so a shape drift reddens.
    ``extra="forbid"`` (no default "ignore") so a leaked field reddens too,
    not just a dropped one. No ``= None`` defaults on nullable fields —
    production emits every key unconditionally, so absence must redden.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    context_id: str | None
    status: str
    type: str
    tool_name: str
    owner: str
    created_at: str
    updated_at: str | None
    request_data: dict[str, Any] | None
    response_data: dict[str, Any] | None
    error_message: str | None
    associated_objects: list[dict[str, Any]]


class CompleteTaskWireResponse(BaseModel):
    """Full success-path wire shape for complete_task — every returned field declared.

    ``extra="forbid"`` so a leaked field reddens too, not just a dropped one.
    No ``= None`` defaults: production emits every key unconditionally, so
    absence must redden (drop half of the forbid claim).
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    status: str
    message: str | None
    completed_at: str | None
    completed_by: str | None


class TaskManagementEnv(IntegrationEnv):
    """Integration test environment for list_tasks.

    No patches -- list_tasks reads real WorkflowStep rows via WorkflowUoW.
    """

    # Dispatch declaration: the base owns call_mcp/call_a2a.
    # Dispatch declaration: the base owns call_mcp/call_a2a, and
    # this env now JOINS the client core — production's list_tasks emits the
    # pinned-required query_summary + pagination, so the core's pinned parse
    # succeeds. list_tasks is MCP-only (no A2A skill, no REST route).
    MCP_TOOL = "list_tasks"
    RESPONSE_MODEL = dict

    EXTERNAL_PATCHES: dict[str, str] = {}

    def _configure_mocks(self) -> None:
        """No mocks needed -- real WorkflowUoW."""

    def _response_cls(self, tool: str) -> type[BaseModel]:
        return GetTaskWireResponse if tool == "get_task" else CompleteTaskWireResponse

    def call_impl(self, **kwargs: Any) -> dict[str, Any]:
        """Call list_tasks directly with real DB (no transport dispatch)."""
        import asyncio

        from src.core.tools.task_management import list_tasks

        self._commit_factory_data()
        identity = kwargs.pop("identity", self.identity)
        return asyncio.run(list_tasks(identity=identity, **kwargs))
