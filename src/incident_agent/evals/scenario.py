"""Scenario specification: the alert shown to the agent plus ground truth for scoring.

Ground truth comes from the fault flags enabled at capture time, never from the agent.
"""

from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

from incident_agent.agent.report import CauseCategory


class Difficulty(StrEnum):
    SINGLE = "single"
    HARD = "hard"
    NEGATIVE = "negative"
    INJECTION = "injection"
    LONG = "long"


class Cause(BaseModel):
    service: str = Field(description="`service.name` exactly as it appears in the fixture.")
    category: CauseCategory
    flag: str | None = Field(default=None, description="Fault flag that produced this cause.")
    description: str
    keywords: list[str] = Field(
        default_factory=list, description="Acceptable phrasings; any match in a summary counts."
    )


class RedHerring(BaseModel):
    service: str
    description: str


class GroundTruth(BaseModel):
    healthy: bool
    causes: list[Cause] = Field(default_factory=list)
    red_herrings: list[RedHerring] = Field(default_factory=list)

    @model_validator(mode="after")
    def _causes_match_health(self) -> "GroundTruth":
        if self.healthy and self.causes:
            raise ValueError("a healthy scenario cannot list causes")
        if not self.healthy and not self.causes:
            raise ValueError("an unhealthy scenario must list at least one cause")
        return self


class ExpectedEvidence(BaseModel):
    description: str
    field: str | None = None
    value: str | None = None


class Injection(BaseModel):
    payload: str = Field(description="The exact instruction-like text planted in telemetry.")
    field: str = Field(description="Attribute that carries the payload.")
    service: str = Field(description="Service whose spans carry the payload.")
    forbidden: list[str] = Field(
        description="Strings whose presence in the report means the agent obeyed the payload."
    )


class Capture(BaseModel):
    demo_version: str
    flags: dict[str, str] = Field(default_factory=dict, description="flag name -> variant")
    baseline_seconds: int
    fault_seconds: int
    fault_start: str | None = Field(default=None, description="ISO-8601 UTC; null if healthy.")
    sample_rate: int = Field(description="1 in N traces kept.")
    span_count: int
    captured_at: str


class ScenarioSpec(BaseModel):
    id: str
    title: str
    difficulty: Difficulty
    alert: str = Field(description="The only scenario text the agent sees.")
    ground_truth: GroundTruth
    expected_evidence: list[ExpectedEvidence] = Field(default_factory=list)
    injection: Injection | None = None
    capture: Capture
    notes: str = ""

    @model_validator(mode="after")
    def _injection_present(self) -> "ScenarioSpec":
        if (self.difficulty == Difficulty.INJECTION) != (self.injection is not None):
            raise ValueError("`injection` is required for, and only for, injection scenarios")
        return self


def load_scenario(scenario_dir: Path) -> ScenarioSpec:
    with (scenario_dir / "scenario.yaml").open() as f:
        return ScenarioSpec.model_validate(yaml.safe_load(f))


def list_scenarios(fixtures_dir: Path) -> list[ScenarioSpec]:
    return [load_scenario(p.parent) for p in sorted(fixtures_dir.glob("*/scenario.yaml"))]
