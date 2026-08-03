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
from tests.utils.workflow_task_seed import create_principal_owned_workflow_step


class GetTaskWireResponse(BaseModel):
    """Minimal success-path wire shape for get_task (extra fields allowed)."""

    model_config = ConfigDict(extra="allow")

    task_id: str
    status: str
    context_id: str | None = None


class CompleteTaskWireResponse(BaseModel):
    """Minimal success-path wire shape for complete_task."""

    model_config = ConfigDict(extra="allow")

    task_id: str
    status: str
    message: str | None = None
    completed_at: str | None = None
    completed_by: str | None = None


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
