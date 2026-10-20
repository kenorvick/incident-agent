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


# Written definitions of the categories. The eval's ground-truth labels are applied under these
# same definitions (see docs/lessons-learned.md, Step A). They are shown to the agent in the
# `submit_report` schema (`category_description`; Step C of Phase 6).
CATEGORY_DEFINITIONS: dict[CauseCategory, str] = {
    CauseCategory.ERROR_RATE: (
        "The culprit service's own code fails a share of requests: errors raised inside the "
        "service itself, not explained by a failing downstream call or by specific bad input or "
        "configuration."
    ),
    CauseCategory.LATENCY: (
        "The culprit service's own code path is slow for some or all requests (for example a "
        "slow branch for certain inputs), with no visible resource pressure and no slow "
        "downstream dependency explaining it."
    ),
    CauseCategory.RESOURCE_EXHAUSTION: (
        "The service degrades because a resource is saturated or growing: memory, heap or GC, "
        "CPU, a cache or queue that keeps growing, a connection pool, or lock contention on its "
        "datastore. Often shows as latency or failures that worsen over time."
    ),
    CauseCategory.DEPENDENCY_FAILURE: (
        "Calls from the culprit service to a downstream service or datastore fail, cannot "
        "connect (bad address, DNS or resolver errors, connection refused) or time out. The "
        "culprit is the caller whose dependency is broken or misaddressed."
    ),
    CauseCategory.BAD_DATA_CONFIG: (
        "Failures are triggered by specific input data or configuration: one particular ID or "
        "value, invalid tokens or credentials, malformed requests, wrong settings."
    ),
}


class Verdict(StrEnum):
    ROOT_CAUSE_FOUND = "root_cause_found"
    NO_SIGNIFICANT_ISSUE = "no_significant_issue"
    INCONCLUSIVE = "inconclusive"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


def category_description() -> str:
    """The `category` field's description for the agent: one line per category, from the dict."""
    lines = "\n".join(f"- {c.value}: {text}" for c, text in CATEGORY_DEFINITIONS.items())
    return f"Category of the cause. Pick the best fit:\n{lines}"


class SuspectedCause(BaseModel):
    service: str = Field(description="The `service.name` value of the service at fault.")
    category: CauseCategory = Field(description=category_description())
    summary: str = Field(description="One or two sentences: what is wrong and how it shows up.")


class Evidence(BaseModel):
    claim: str = Field(description="A single factual observation supporting the conclusion.")
    tool_call_id: str = Field(
        description=(
            "Ref of the tool call whose result shows this: the `[ref cN]` line at the top of "
            "that result, written as `cN` (for example `c3`)."
        )
    )
    cited_values: list[str] = Field(
        default_factory=list,
        description=(
            "Values that support the claim, each ONE value copied verbatim from that tool result "
            "(a number, an ID, or a field value), never a phrase that joins labels and numbers. "
            'Good: ["OLJCESPC7Z", "58"]. Bad: ["demo.product.id OLJCESPC7Z selection 100% (58)"]. '
            "Put the labels in `claim`."
        ),
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
