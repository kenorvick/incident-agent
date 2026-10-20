"""Render an `EvalRun` as Markdown and append it to the eval results log.

`append_entry` only ever appends: the header is written once when the file is new, existing
entries are never touched, and appending a run id that is already present is refused (unless
explicitly allowed). Every entry lists failures and partial/errored runs; nothing is filtered.

Conventions: pass/service/category columns are `k/n` over *scored* runs (completed + errored;
runs cut off by the spend cap are not attempts and are listed separately); means are over
completed runs only; `grounded` is pooled across runs (evidence items grounded / evidence items
cited); `category` counts the primary label or an accepted alternative and `exact` the primary
label only (`-` for runs scored before that field existed); `fab` is the number of
runs with any fabricated value or unknown tool_call_id in the *submitted* report. Since the
agent's `submit_report` verifies citations (production guardrail), `fab` measures what survives
it; `runs with citation rejection` counts how many runs needed a correction.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import Any

from incident_agent.evals.runner import EvalRun, RunRecord, RunStatus, git_state
from incident_agent.evals.scenario import Difficulty

__all__ = ["DEFAULT_RESULTS_PATH", "ScenarioStats", "append_entry", "compare", "render_entry"]

DEFAULT_RESULTS_PATH = Path("docs/eval-results.md")
HEADER = """# Eval results

Append-only log of eval runs: one entry per run, newest last. Entries are never edited or
removed, and they include failures and partial runs. Scores are deterministic (see
`src/incident_agent/evals/scoring.py`); no LLM judge is used. Pass rates are `k/n` over the
scored runs of each scenario.
"""


# --- statistics --------------------------------------------------------------------------


@dataclass
class ScenarioStats:
    scenario_id: str
    difficulty: Difficulty
    planned: int
    scored: int
    passed: int
    errored: int
    service_k: int
    service_n: int
    category_k: int
    category_n: int
    exact_k: int  # primary category hit (diagnostic)
    exact_n: int  # runs that recorded `category_exact` (older logs/scores did not)
    evidence_total: int
    evidence_grounded: int
    fabricated_runs: int
    false_alarms: int
    false_alarm_n: int
    injections_followed: int
    injection_n: int
    herrings_blamed: int
    herring_n: int
    mean_steps: float | None
    mean_tool_calls: float | None
    mean_input_tokens: float | None
    mean_output_tokens: float | None
    mean_cost_usd: float | None

    @property
    def mixed(self) -> bool:
        """Some runs passed and some did not (only meaningful with more than one run)."""
        return self.scored > 1 and 0 < self.passed < self.scored


def _mean(values: list[float]) -> float | None:
    return fmean(values) if values else None


def _count(records: Iterable[RunRecord], field: str) -> tuple[int, int]:
    """(true count, applicable count) of a nullable boolean score field."""
    vals = [v for r in records if (v := getattr(r.score, field)) is not None]
    return sum(vals), len(vals)


def scenario_stats(run: EvalRun) -> list[ScenarioStats]:
    out: list[ScenarioStats] = []
    for sid, difficulty in run.scenario_difficulty.items():
        recs = [r for r in run.scored_records if r.scenario_id == sid]
        done = [r for r in recs if r.status == RunStatus.COMPLETED]
        svc_k, svc_n = _count(recs, "service_correct")
        cat_k, cat_n = _count(recs, "category_correct")
        exact_k, exact_n = _count(recs, "category_exact")
        fa_k, fa_n = _count(recs, "false_alarm")
        inj_k, inj_n = _count(recs, "followed_injection")
        rh_k, rh_n = _count(recs, "blamed_red_herring")
        out.append(
            ScenarioStats(
                scenario_id=sid,
                difficulty=difficulty,
                planned=run.runs_per_scenario,
                scored=len(recs),
                passed=sum(r.score.passed for r in recs),
                errored=sum(r.score.errored for r in recs),
                service_k=svc_k,
                service_n=svc_n,
                category_k=cat_k,
                category_n=cat_n,
                exact_k=exact_k,
                exact_n=exact_n,
                evidence_total=sum(r.score.evidence_total for r in recs),
                evidence_grounded=sum(r.score.evidence_grounded for r in recs),
                fabricated_runs=sum(not r.score.fabrication_free for r in recs),
                false_alarms=fa_k,
                false_alarm_n=fa_n,
                injections_followed=inj_k,
                injection_n=inj_n,
                herrings_blamed=rh_k,
                herring_n=rh_n,
                mean_steps=_mean([r.score.steps for r in done]),
                mean_tool_calls=_mean([r.score.tool_calls for r in done]),
                mean_input_tokens=_mean([r.score.input_tokens for r in done]),
                mean_output_tokens=_mean([r.score.output_tokens for r in done]),
                mean_cost_usd=_mean([r.score.cost_usd for r in done]),
            )
        )
    return out


def _rate(k: int, n: int) -> float | None:
    return k / n if n else None


def _headline(run: EvalRun) -> dict[str, float | None]:
    """Run-level metrics shared by the aggregate table and `compare`."""
    stats = scenario_stats(run)
    done = [r for r in run.scored_records if r.status == RunStatus.COMPLETED]
    ev_total = sum(s.evidence_total for s in stats)
    scored = sum(s.scored for s in stats)
    metrics: dict[str, float | None] = {
        "pass rate (overall)": _rate(sum(s.passed for s in stats), scored),
    }
    for d in Difficulty:
        sub = [s for s in stats if s.difficulty == d]
        metrics[f"pass rate ({d.value})"] = _rate(
            sum(s.passed for s in sub), sum(s.scored for s in sub)
        )
    metrics["service correct"] = _rate(
        sum(s.service_k for s in stats), sum(s.service_n for s in stats)
    )
    metrics["category correct"] = _rate(
        sum(s.category_k for s in stats), sum(s.category_n for s in stats)
    )
    metrics["category exact (primary only)"] = _rate(
        sum(s.exact_k for s in stats), sum(s.exact_n for s in stats)
    )
    metrics["evidence grounded"] = _rate(sum(s.evidence_grounded for s in stats), ev_total)
    metrics["runs with fabrication"] = _rate(sum(s.fabricated_runs for s in stats), scored)
    metrics["runs with citation rejection"] = _rate(
        sum(r.score.citation_rejections > 0 or r.score.citation_forced for r in run.scored_records),
        scored,
    )
    metrics["mean rejected submissions per run"] = _mean(
        [r.score.citation_rejections for r in done]
    )
    metrics["errored runs"] = _rate(sum(s.errored for s in stats), scored)
    metrics["mean steps"] = _mean([r.score.steps for r in done])
    metrics["mean tool calls"] = _mean([r.score.tool_calls for r in done])
    metrics["mean tokens (in+out)"] = _mean(
        [r.score.input_tokens + r.score.output_tokens for r in done]
    )
    metrics["mean cost per run (USD)"] = _mean([r.score.cost_usd for r in run.records])
    return metrics


# --- formatting --------------------------------------------------------------------------


def _frac(k: int, n: int) -> str:
    return f"{k}/{n}" if n else "-"


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:.0f}%"


def _num(x: float | None, digits: int = 1) -> str:
    return "-" if x is None else f"{x:.{digits}f}"


def _tokens(x: float | None) -> str:
    if x is None:
        return "-"
    return f"{x / 1000:.1f}k" if x >= 1000 else f"{x:.0f}"


def _usd(x: float | None) -> str:
    return "-" if x is None else f"${x:.4f}" if x < 1 else f"${x:.2f}"


def _date(iso: str | None) -> str:
    if not iso:
        return "unknown"
    return datetime.fromisoformat(iso).astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _flags(s: ScenarioStats) -> str:
    parts = []
    if s.false_alarm_n:
        parts.append(f"false alarm {_frac(s.false_alarms, s.false_alarm_n)}")
    if s.injection_n:
        parts.append(f"followed injection {_frac(s.injections_followed, s.injection_n)}")
    if s.herring_n:
        parts.append(f"blamed red herring {_frac(s.herrings_blamed, s.herring_n)}")
    return "; ".join(parts) or "-"


def _scenario_table(stats: list[ScenarioStats]) -> str:
    header = [
        "scenario", "difficulty", "runs", "pass", "service", "category", "exact", "grounded", "fab",
        "flags", "steps", "tools", "tokens in/out", "cost",
    ]  # fmt: skip
    rows = []
    for s in stats:
        rows.append(
            [
                s.scenario_id,
                s.difficulty.value,
                f"{s.scored}/{s.planned}" if s.scored != s.planned else str(s.scored),
                _frac(s.passed, s.scored),
                _frac(s.service_k, s.service_n),
                _frac(s.category_k, s.category_n),
                _frac(s.exact_k, s.exact_n),
                _pct(_rate(s.evidence_grounded, s.evidence_total)),
                _frac(s.fabricated_runs, s.scored),
                _flags(s),
                _num(s.mean_steps),
                _num(s.mean_tool_calls),
                f"{_tokens(s.mean_input_tokens)} / {_tokens(s.mean_output_tokens)}",
                _usd(s.mean_cost_usd),
            ]
        )
    return _table(header, rows)


def _aggregate_table(run: EvalRun) -> str:
    stats = scenario_stats(run)
    rows = [
        [
            "overall",
            _frac(sum(s.passed for s in stats), sum(s.scored for s in stats)),
            _pct(_headline(run)["pass rate (overall)"]),
        ]
    ]
    for d in Difficulty:
        sub = [s for s in stats if s.difficulty == d]
        if sub:
            k, n = sum(s.passed for s in sub), sum(s.scored for s in sub)
            rows.append([f"{d.value} ({len(sub)} scenarios)", _frac(k, n), _pct(_rate(k, n))])
    return _table(["aggregate", "pass", "rate"], rows)


def _variance_note(stats: list[ScenarioStats]) -> str:
    multi = [s for s in stats if s.scored > 1]
    if not multi:
        return "Variance: every scenario has a single run, so run-to-run variance is not measured."
    mixed = [s for s in multi if s.mixed]
    line = (
        f"Variance: {len(mixed)} of {len(multi)} scenarios with more than one run "
        "had mixed outcomes"
    )
    if mixed:
        line += (
            " (" + ", ".join(f"{s.scenario_id} {_frac(s.passed, s.scored)}" for s in mixed) + ")"
        )
    return line + "."


def _config_lines(run: EvalRun, extra: dict[str, Any] | None) -> str:
    items = {**run.agent_config, **run.config, **(extra or {})}
    flags = ", ".join(f"`{k}={v}`" for k, v in items.items()) or "none recorded"
    return f"{flags}; runs per scenario `{run.runs_per_scenario}`; concurrency `{run.concurrency}`"


def _commit(run: EvalRun) -> str:
    commit, dirty = run.git_commit, run.git_dirty
    if commit is None:
        commit, dirty = git_state()
    if commit is None:
        return "unknown"
    return f"`{commit[:7]}`" + (" (dirty: uncommitted changes)" if dirty else "")


def _failures(run: EvalRun) -> str:
    lines = []
    for r in run.records:
        if r.status == RunStatus.BUDGET_STOPPED:
            lines.append(
                f"- `{r.scenario_id}` run {r.attempt}: cut off by the spend cap (not scored)"
            )
        elif not r.score.passed:
            why = "; ".join(r.score.failure_reasons) or "failed"
            lines.append(f"- `{r.scenario_id}` run {r.attempt}: {why} (log: `{r.log_path}`)")
    return "\n".join(lines) if lines else "None: every scored run passed."


# --- public ------------------------------------------------------------------------------


def render_entry(
    run: EvalRun,
    *,
    notes: str | None = None,
    config: dict[str, Any] | None = None,
    compare_to: EvalRun | None = None,
) -> str:
    stats = scenario_stats(run)
    n_records = len(run.records)
    cost = run.total_cost_usd
    note = run.notes if notes is None else notes
    lines = [
        f"## {run.run_id} - {_date(run.started_at)}",
        "",
        f"<!-- eval-run: {run.run_id} -->",
        "",
    ]
    if run.partial:
        lines += [
            f"**PARTIAL RUN.** {run.partial_reason or 'stopped early'}. "
            f"{len(run.scored_records)} of {run.planned_runs} planned runs were scored; "
            "pass rates below cover only those.",
            "",
        ]
    elif not run.finished:
        lines += [
            "**INCOMPLETE RUN.** The process stopped before finishing; results are as saved.",
            "",
        ]
    if run.rescored_at:
        lines += [f"Rescored {_date(run.rescored_at)} from saved logs (no model calls).", ""]
    lines += [
        f"- **Commit:** {_commit(run)}",
        f"- **Model:** {run.provider or 'unknown'} / `{run.model_id or 'unknown'}`",
        f"- **Config:** {_config_lines(run, config)}",
        f"- **Runs:** {n_records} executed of {run.planned_runs} planned "
        f"({len(run.scenario_difficulty)} scenarios); "
        f"{sum(r.score.errored for r in run.scored_records)} errored",
        f"- **Cost:** {_usd(cost)} total, {_usd(cost / n_records if n_records else None)} per run"
        + (
            f" (ledger total; step logs sum to {_usd(run.log_cost_usd)})"
            if run.ledger_cost_usd is not None
            else " (summed from step logs)"
        ),
    ]
    if note.strip():
        lines += [f"- **Notes:** {note.strip()}"]
    lines += ["", "### Per scenario", "", _scenario_table(stats), "", "### Aggregate", ""]
    lines += [_aggregate_table(run), "", _variance_note(stats), ""]
    if run.not_completed:
        lines += ["Not completed: " + ", ".join(f"`{x}`" for x in run.not_completed) + ".", ""]
    lines += ["### Failures", "", _failures(run), ""]
    if compare_to is not None:
        lines += [compare(compare_to, run), ""]
    return "\n".join(lines)


def compare(before: EvalRun, after: EvalRun) -> str:
    """Before/after delta table. Rates are compared as rates, so runs with different N or
    scenario sets can be compared, but a note says when they differ."""
    a, b = _headline(before), _headline(after)
    rows = []
    for key in a:
        x, y = a[key], b[key]
        is_rate = key.startswith(
            ("pass rate", "service", "category", "evidence", "runs with", "errored")
        )
        fmt = _pct if is_rate else (lambda v, k=key: _usd(v) if "USD" in k else _num(v, 1))
        if x is None or y is None:
            delta = "-"
        elif is_rate:
            delta = f"{(y - x) * 100:+.1f} pp"
        else:
            delta = f"{y - x:+.4g}"
        rows.append([key, fmt(x), fmt(y), delta])
    sb, sa = (
        {s.scenario_id: s for s in scenario_stats(before)},
        {s.scenario_id: s for s in scenario_stats(after)},
    )
    per = []
    for sid in dict.fromkeys([*sb, *sa]):
        x, y = sb.get(sid), sa.get(sid)
        xs = _frac(x.passed, x.scored) if x else "-"
        ys = _frac(y.passed, y.scored) if y else "-"
        change = ""
        if x and y and x.scored and y.scored:
            d = y.passed / y.scored - x.passed / x.scored
            change = "better" if d > 0 else "worse" if d < 0 else "same"
        per.append([sid, xs, ys, change])
    notes = []
    if before.runs_per_scenario != after.runs_per_scenario or set(
        before.scenario_difficulty
    ) != set(after.scenario_difficulty):
        notes.append(
            "Runs per scenario or scenario sets differ; compare the rates, not the counts."
        )
    if before.partial or after.partial:
        notes.append("At least one of these runs is partial.")
    out = [
        f"### Change: `{before.run_id}` -> `{after.run_id}`",
        "",
        f"Before: {_commit(before)}, {before.model_id or 'unknown'}. "
        f"After: {_commit(after)}, {after.model_id or 'unknown'}.",
        "",
        _table(["metric", "before", "after", "delta"], rows),
        "",
        _table(["scenario (pass)", "before", "after", "change"], per),
    ]
    if notes:
        out += ["", *notes]
    return "\n".join(out)


def append_entry(
    run: EvalRun,
    path: Path = DEFAULT_RESULTS_PATH,
    *,
    notes: str | None = None,
    config: dict[str, Any] | None = None,
    compare_to: EvalRun | None = None,
    allow_duplicate: bool = False,
) -> str:
    """Append one entry to the results log (creating it, with its header, if needed) and return
    the entry text. Never rewrites existing content."""
    path = Path(path)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    marker = f"<!-- eval-run: {run.run_id} -->"
    if marker in existing and not allow_duplicate:
        raise ValueError(f"{path} already has an entry for run {run.run_id}")
    entry = render_entry(run, notes=notes, config=config, compare_to=compare_to)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        if not existing.strip():
            fh.write(HEADER)
        fh.write("\n" + entry.rstrip() + "\n")
    return entry
