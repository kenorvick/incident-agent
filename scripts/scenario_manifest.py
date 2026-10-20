"""Shared access to infra/scenarios.yaml for the capture scripts."""

import re
from pathlib import Path
from typing import Any

import yaml

REPO_DIR = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_DIR / "infra" / "scenarios.yaml"
DEMO_FLAGS_PATH = REPO_DIR / "infra" / "opentelemetry-demo" / "src" / "flagd" / "demo.flagd.json"

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Fields copied verbatim from a manifest entry into fixtures/<id>/scenario.yaml. Everything else in
# an entry is capture plan only.
SPEC_FIELDS = (
    "id",
    "title",
    "difficulty",
    "alert",
    "ground_truth",
    "expected_evidence",
    "injection",
    "notes",
)

# Sample rates the finaliser may escalate through to stay inside the size budget. Planted
# (injection) traces are chosen to survive every one of them.
SAMPLE_RATE_LADDER = (1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64)


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    with path.open() as f:
        manifest = yaml.safe_load(f)
    if not isinstance(manifest, dict) or "scenarios" not in manifest:
        raise SystemExit(f"{path}: expected a mapping with a `scenarios` list")
    return manifest


def scenario_ids(manifest: dict[str, Any]) -> list[str]:
    return [s["id"] for s in manifest["scenarios"]]


def get_scenario(manifest: dict[str, Any], scenario_id: str) -> dict[str, Any]:
    """The manifest entry with `defaults` applied. Unknown ids list the valid ones."""
    for entry in manifest["scenarios"]:
        if entry["id"] == scenario_id:
            merged = {**manifest.get("defaults", {}), **entry}
            merged["flags"] = {str(k): str(v) for k, v in (merged.get("flags") or {}).items()}
            return merged
    raise SystemExit(
        f"unknown scenario id {scenario_id!r}. Known ids:\n  " + "\n  ".join(scenario_ids(manifest))
    )


def build_spec_dict(scenario: dict[str, Any], capture: dict[str, Any]) -> dict[str, Any]:
    """Assemble the dict that must validate as `ScenarioSpec` for one finished capture."""
    spec = {k: scenario[k] for k in SPEC_FIELDS if scenario.get(k) is not None}
    spec["capture"] = capture
    return spec
