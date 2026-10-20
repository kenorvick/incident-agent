"""The investigation loop: call the model, run the tools it asks for, repeat until it submits.

History is a plain list of messages, appended to and never rewritten (context management belongs
in `context.py` once measured failures motivate it). Every way a run can end, including limits and
errors, produces an `InvestigationResult` with a report; nothing here is allowed to crash a run.
"""

from __future__ import annotations

import time
from pathlib import Path

import anyio
from pydantic import BaseModel, Field

from incident_agent.agent.models.base import (
    BudgetExceededError,
    Message,
    ModelClient,
    ModelResponse,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)
from incident_agent.agent.prompts import load_system_prompt
from incident_agent.agent.report import (
    Confidence,
    InvestigationReport,
    InvestigationResult,
    TerminatedReason,
    Verdict,
)
from incident_agent.agent.tools import (
    SUBMIT_REPORT,
    CompositeToolExecutor,
    SubmitReportTool,
    ToolExecutor,
    call_ref,
    with_ref,
)
from incident_agent.agent.tracing import StepLogger

MAX_CONSECUTIVE_CUTOFFS = 2

NO_SUBMIT_NUDGE = (
    f"You have not submitted a report. Finish by calling `{SUBMIT_REPORT}` now with your "
    "conclusion, using the verdict `inconclusive` if you could not reach one."
)
CUTOFF_NUDGE = (
    "Your last response was cut off by the output limit. Be more concise: continue by calling a "
    f"tool, or call `{SUBMIT_REPORT}` if you are ready to conclude."
)


class AgentConfig(BaseModel):
    """Limits for one investigation. Hitting one ends the run with an inconclusive report."""

    max_steps: int = Field(default=15, ge=1, description="Model calls, including nudges.")
    max_input_tokens: int | None = Field(
        default=400_000, description="Total input tokens across all model calls (cache included)."
    )
    max_cost_usd: float | None = Field(default=0.75, description="Spend for this investigation.")
    timeout_seconds: float | None = Field(default=300.0, description="Wall-clock limit.")
    max_tokens: int = Field(default=16_000, description="Per response, reasoning included.")
    verify_citations: bool = Field(
        default=True,
        description="`submit_report` rejects citations not found in the cited tool result.",
    )
    max_citation_rejections: int = Field(
        default=2, ge=0, description="Rejected submissions before one is accepted as it is."
    )


async def investigate(
    alert: str,
    *,
    tools: ToolExecutor,
    model: ModelClient,
    config: AgentConfig | None = None,
    log_path: Path,
    run_id: str,
    scenario_id: str | None = None,
) -> InvestigationResult:
    """Investigate `alert` with `tools` until a report is submitted or a limit is hit."""
    config = config or AgentConfig()
    started = time.monotonic()
    with StepLogger(log_path) as log:
        submit = SubmitReportTool(
            verify_citations=config.verify_citations,
            max_citation_rejections=config.max_citation_rejections,
            on_check=log.citation_check,
        )
        run = _Run(config, CompositeToolExecutor([submit], tools), model, submit, log)
        log.run_start(
            run_id=run_id,
            scenario_id=scenario_id,
            alert=alert,
            provider=_attr(model, "provider"),
            model_id=_attr(model, "model"),
            config=config.model_dump(),
        )
        try:
            with anyio.fail_after(config.timeout_seconds):
                await run.loop(alert)
        except TimeoutError:
            run.stop(TerminatedReason.BUDGET, f"Time limit of {config.timeout_seconds:g}s reached.")
        except BudgetExceededError as exc:
            run.stop(TerminatedReason.BUDGET, f"Spend cap reached: {exc}")
        except Exception as exc:
            run.stop(TerminatedReason.ERROR, f"{type(exc).__name__}: {exc}")
        return run.finish(int((time.monotonic() - started) * 1000), log_path)


def _attr(model: ModelClient, name: str) -> str | None:
    """Provider/model name for the log, looking through the spend-guard wrapper."""
    for obj in (model, getattr(model, "inner", None)):
        if isinstance(value := getattr(obj, name, None), str):
            return value
    return None


class _Run:
    """State of one investigation."""

    def __init__(
        self,
        config: AgentConfig,
        tools: CompositeToolExecutor,
        model: ModelClient,
        submit: SubmitReportTool,
        log: StepLogger,
    ) -> None:
        self.config = config
        self.log = log
        self.tools = tools
        self.specs: list[ToolSpec] = tools.list_tools()
        self.model = model
        self.submit = submit
        self.system = load_system_prompt()
        self.messages: list[Message] = []
        self.steps = 0
        self.tool_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd = 0.0
        self.reason = TerminatedReason.ERROR
        self.error: str | None = None
        self._nudged = False
        self._cutoffs = 0

    def stop(self, reason: TerminatedReason, error: str | None) -> None:
        self.reason, self.error = reason, error

    def _limit_reached(self) -> str | None:
        cfg = self.config
        if self.steps >= cfg.max_steps:
            return f"Step limit reached ({cfg.max_steps} model calls)."
        if cfg.max_input_tokens is not None and self.input_tokens >= cfg.max_input_tokens:
            return f"Input token limit reached ({self.input_tokens} >= {cfg.max_input_tokens})."
        if cfg.max_cost_usd is not None and self.cost_usd >= cfg.max_cost_usd:
            return f"Cost limit reached (${self.cost_usd:.4f} >= ${cfg.max_cost_usd:.2f})."
        return None

    async def loop(self, alert: str) -> None:
        self.messages.append(Message.user(f"Alert:\n\n{alert}"))
        while True:
            if (limit := self._limit_reached()) is not None:
                reason = (
                    TerminatedReason.MAX_STEPS
                    if self.steps >= self.config.max_steps
                    else TerminatedReason.BUDGET
                )
                return self.stop(reason, limit)
            response = await self.model.complete(
                self.system, self.messages, self.specs, self.config.max_tokens
            )
            self._record_response(response)
            if response.tool_calls:
                self._cutoffs = 0
                await self._run_tools(response.tool_calls)
                if self.submit.report is not None:
                    return self.stop(TerminatedReason.SUBMITTED, None)
            elif response.stop_reason == "max_tokens":
                self._cutoffs += 1
                if self._cutoffs > MAX_CONSECUTIVE_CUTOFFS:
                    return self.stop(
                        TerminatedReason.ERROR,
                        f"Responses were cut off by the {self.config.max_tokens}-token output "
                        f"limit {self._cutoffs} times in a row.",
                    )
                self.messages.append(Message.user(CUTOFF_NUDGE))
            elif not self._nudged:
                self._nudged = True
                self.messages.append(Message.user(NO_SUBMIT_NUDGE))
            else:
                return self.stop(
                    TerminatedReason.ERROR,
                    f"The model ended its turn without calling {SUBMIT_REPORT}, even after a "
                    "reminder.",
                )

    def _record_response(self, response: ModelResponse) -> None:
        self.steps += 1
        self.input_tokens += response.usage.total_input_tokens
        self.output_tokens += response.usage.output_tokens
        self.cost_usd += response.cost_usd
        self.log.model_call(self.steps, response)
        message = response.to_message()
        if not message.content:  # providers reject empty assistant turns
            message.content = [TextBlock(text="(no output)")]
        self.messages.append(message)

    async def _run_tools(self, calls: list[ToolUseBlock]) -> None:
        """Run every requested call concurrently; results keep the order of the calls."""
        outputs: list[tuple[str, bool, float]] = [("", True, 0.0)] * len(calls)

        async def one(i: int, call: ToolUseBlock) -> None:
            outputs[i] = await self._call_one(call)

        async with anyio.create_task_group() as tg:
            for i, call in enumerate(calls):
                tg.start_soon(one, i, call)

        results = []
        for i, (call, (text, is_error, latency_ms)) in enumerate(zip(calls, outputs, strict=True)):
            ref = call_ref(self.tool_calls + i + 1)  # memory's ToolCallLedger counts the same way
            shown = with_ref(ref, text)
            if call.name != SUBMIT_REPORT:
                self.submit.note_result(ref, text)
            self.log.tool_result(
                step=self.steps,
                tool_call_id=call.id,
                ref=ref,
                name=call.name,
                arguments=call.input,
                content=shown,
                is_error=is_error,
                latency_ms=latency_ms,
            )
            results.append(ToolResultBlock(tool_use_id=call.id, content=shown, is_error=is_error))
        self.tool_calls += len(calls)
        self.messages.append(Message.tool_results(results))

    async def _call_one(self, call: ToolUseBlock) -> tuple[str, bool, float]:
        started = time.perf_counter()
        text, is_error = await self._execute(call)
        return text, is_error, (time.perf_counter() - started) * 1000

    async def _execute(self, call: ToolUseBlock) -> tuple[str, bool]:
        if call.parse_error:
            return (
                f"Could not parse the arguments for `{call.name}`: {call.parse_error}. "
                "Send a single valid JSON object that matches the tool's input schema.",
                True,
            )
        names = [s.name for s in self.specs]
        if call.name not in names:
            return f"Unknown tool `{call.name}`. Available tools: {', '.join(names)}.", True
        try:
            return await self.tools.call(call.name, call.input)
        except Exception as exc:  # a tool bug must not end the investigation
            return f"Tool `{call.name}` failed: {type(exc).__name__}: {exc}", True

    def finish(self, duration_ms: int, log_path: Path) -> InvestigationResult:
        if self.submit.report is not None:  # submitted even if a limit fired right after
            self.stop(TerminatedReason.SUBMITTED, None)
            report = self.submit.report
        else:
            report = _inconclusive_report(self.error or "The investigation ended early.")
        self.log.final(report.model_dump(mode="json"), self.reason.value)
        self.log.run_end(
            steps=self.steps,
            tool_calls=self.tool_calls,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cost_usd=self.cost_usd,
            duration_ms=duration_ms,
        )
        return InvestigationResult(
            report=report,
            terminated_reason=self.reason,
            steps=self.steps,
            tool_calls=self.tool_calls,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cost_usd=self.cost_usd,
            duration_ms=duration_ms,
            log_path=log_path,
            error=self.error,
        )


def _inconclusive_report(why: str) -> InvestigationReport:
    """The report synthesized when the agent never submitted one."""
    return InvestigationReport(
        verdict=Verdict.INCONCLUSIVE,
        confidence=Confidence.LOW,
        uncertainties=[
            f"The investigation stopped before a report was submitted: {why}",
            "No root cause was established. This says nothing about whether the system is healthy.",
        ],
    )


__all__ = ["AgentConfig", "investigate"]
