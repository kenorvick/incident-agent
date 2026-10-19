"""Adapter from `investigate` to the eval harness's `Investigator` signature."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from incident_agent.agent.loop import AgentConfig, investigate
from incident_agent.agent.models import ModelClient, get_model_client, spend_label
from incident_agent.agent.report import InvestigationResult
from incident_agent.agent.tools import McpToolExecutor, ToolExecutor
from incident_agent.evals.scenario import ScenarioSpec

if TYPE_CHECKING:
    from incident_agent.agent.memory import MemoryConfig
    from incident_agent.evals.runner import Investigator


def make_investigator(
    config: AgentConfig | None = None,
    *,
    model_factory: Callable[[], ModelClient] = get_model_client,
    fixtures_dir: Path | None = None,
    memory: MemoryConfig | None = None,
) -> Investigator:
    """An investigator for the eval runner: each run gets its own in-process MCP session over the
    scenario's fixture and its own model client, and its spend is labelled `run/scenario/attempt`.

    Memory is off unless `memory` is given; then each run also gets the `recall` and `save_memory`
    tools and the memory prompt, with `scenario.id` as the investigation id.
    """
    config = config or AgentConfig()

    async def run(
        scenario: ScenarioSpec, attempt: int, log_path: Path, run_id: str
    ) -> InvestigationResult:
        from incident_agent.mcp_server.server import open_session

        model = model_factory()
        with spend_label(f"{run_id}/{scenario.id}/{attempt}"):
            async with open_session(scenario.id, fixtures_dir=fixtures_dir) as session:
                tools: ToolExecutor = await McpToolExecutor.connect(session)
                if memory is not None:
                    from incident_agent.agent.memory import enable_memory

                    tools, model = enable_memory(
                        tools,
                        model,
                        memory.open_store(log_path),
                        run_id=run_id,
                        investigation_id=scenario.id,
                    )
                return await investigate(
                    scenario.alert,
                    tools=tools,
                    model=model,
                    config=config,
                    log_path=log_path,
                    run_id=run_id,
                    scenario_id=scenario.id,
                )

    return run
