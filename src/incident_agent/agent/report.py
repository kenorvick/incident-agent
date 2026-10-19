"""The agent's final answer: a structured report submitted through the `submit_report` tool.

Scoring reads this model directly, so the agent's conclusion never has to be parsed from prose.
"""

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field


class CauseCategory(StrEnum):
    ERROR_RATE = "error_rate"
    LATENCY = "latency"
    RESOURCE_EXHAUSTION = "resource_exhaustion"
    DEPENDENCY_FAILURE = "dependency_failure"
    BAD_DATA_CONFIG = "bad_data_config"


class Verdict(StrEnum):
    ROOT_CAUSE_FOUND = "root_cause_found"
    NO_SIGNIFICANT_ISSUE = "no_significant_issue"
    INCONCLUSIVE = "inconclusive"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SuspectedCause(BaseModel):
    service: str = Field(description="The `service.name` value of the service at fault.")
    category: CauseCategory
    summary: str = Field(description="One or two sentences: what is wrong and how it shows up.")


class Evidence(BaseModel):
    claim: str = Field(description="A single factual observation supporting the conclusion.")
    tool_call_id: str = Field(description="ID of the tool call whose result shows this.")
    cited_values: list[str] = Field(
        default_factory=list,
        description="Numbers, IDs and field values exactly as they appear in that tool result.",
    )


class InvestigationReport(BaseModel):
    verdict: Verdict
    causes: list[SuspectedCause] = Field(
        default_factory=list,
        description="Suspected root causes, most likely first. Empty unless one was found.",
    )
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: Confidence
    uncertainties: list[str] = Field(
        default_factory=list, description="What remains unverified or could change the conclusion."
    )


class TerminatedReason(StrEnum):
    SUBMITTED = "submitted"
    MAX_STEPS = "max_steps"
    BUDGET = "budget"
    ERROR = "error"


class InvestigationResult(BaseModel):
    """What one investigation run returns. Without a submitted report the loop synthesizes an
    `inconclusive` one, so `report` is always present."""

    report: InvestigationReport
    terminated_reason: TerminatedReason
    steps: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    duration_ms: int
    log_path: Path
    error: str | None = None
