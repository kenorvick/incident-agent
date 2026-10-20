"""Counterfactual run: iteration-2 citation guidance (no per-value rule) WITH the iteration-4
citation check, to see whether the check rescues the failure mode iteration 3 fixed by prompting.

Usage: uv run python scripts/ablate_citation_guidance.py id1,id2 [--dry]
Patches the guidance in memory only (no repo file changes); results go to
runs/ablate-iter2-guidance-plus-check. Spends model money; set SPEND_CAP_USD.
"""

import sys
from pathlib import Path

import anyio

from incident_agent.agent import loop, report
from incident_agent.agent.investigator import make_investigator
from incident_agent.agent.models import SpendLedger, get_model_client
from incident_agent.agent.prompts import load_system_prompt
from incident_agent.backends import default_fixtures_dir
from incident_agent.evals.runner import run_eval, select_scenarios
from incident_agent.evals.scenario import list_scenarios

OLD_FIELD = "Numbers, IDs and field values exactly as they appear in that tool result."
NEW_SENT = (
    "Each entry in `cited_values` is a single value copied verbatim (one number, ID, or field "
    "value), not a phrase combining labels and numbers; put the labels in `claim`. "
)
report.Evidence.model_fields["cited_values"].description = OLD_FIELD
report.Evidence.model_rebuild(force=True)
report.InvestigationReport.model_rebuild(force=True)
prompt = load_system_prompt()
assert NEW_SENT in prompt
old_prompt = prompt.replace(NEW_SENT, "")
loop.load_system_prompt = lambda: old_prompt
assert OLD_FIELD in str(report.InvestigationReport.model_json_schema())
if "--dry" in sys.argv:
    print("patched ok")
    sys.exit()

ledger = SpendLedger()
ids = sys.argv[1].split(",")
specs = select_scenarios(list_scenarios(default_fixtures_dir()), ids=ids)
inv = make_investigator(
    loop.AgentConfig(),
    model_factory=lambda: get_model_client(None, None, ledger=ledger),
    fixtures_dir=default_fixtures_dir(),
)
run = anyio.run(
    lambda: run_eval(
        specs,
        inv,
        runs_per_scenario=3,
        concurrency=3,
        run_id="ablate-iter2-guidance-plus-check",
        runs_dir=Path("runs"),
        notes="Counterfactual: iter2 citation guidance + iter4 check",
        ledger=ledger,
    )
)
for r in run.records:
    s = r.score
    print(
        r.scenario_id,
        r.attempt,
        "pass" if s.passed else "FAIL",
        "rejections",
        s.citation_rejections,
        "forced",
        s.citation_forced,
        "fab",
        len(s.fabricated_values),
        "steps",
        s.steps,
        s.failure_reasons,
    )
