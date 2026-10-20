"""Eval runner: run an injectable investigator over scenarios, N times each, and keep everything.

Layout (all under `runs/<run_id>/`, gitignored):

    scenarios.json            snapshot of the ScenarioSpecs (ground truth) used for this run
    summary.json              the whole `EvalRun`, rewritten atomically after every finished run
    <scenario_id>/<n>.jsonl   the step log the investigator wrote (n starts at 1)
    <scenario_id>/<n>.result.json   that run's `RunRecord` (result + score)

Properties that matter because model spend is capped:
  * Results are persisted as each run finishes, so a crash or Ctrl-C keeps everything completed.
  * Runs are scheduled attempt-major (every scenario once, then every scenario again, ...), so
    a spend-cap stop leaves each scenario with as many attempts as possible, not the first
    few scenarios with all of theirs.
  * `BudgetExceededError` stops scheduling new runs and marks the `EvalRun` partial. A run cut
    off mid-way is kept as a `budget_stopped` record (scored from its log) and excluded from
    pass rates; runs that never started are listed in `not_completed`.
  * A per-run exception becomes an `errored` record (scored as failed) and the suite goes on.
  * `rescore` recomputes every score from the saved logs/results without any model call.
"""

from __future__ import annotations

import json
import subprocess
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import anyio
from pydantic import BaseModel, Field

from incident_agent.agent.models.budget import BudgetExceededError, SpendLedger, spend_label
from incident_agent.agent.report import InvestigationResult
from incident_agent.evals.logs import StepLog, parse_log
from incident_agent.evals.scenario import Difficulty, ScenarioSpec
from incident_agent.evals.scoring import RunScore, score_failed_run, score_run

__all__ = [
    "EvalRun",
    "Investigator",
    "RunRecord",
    "RunStatus",
    "git_state",
    "load_eval_run",
    "memory_enabled",
    "rescore",
    "run_eval",
    "select_scenarios",
]

# (scenario, attempt number starting at 1, where to write the step log, eval run id)
Investigator = Callable[[ScenarioSpec, int, Path, str], Awaitable[InvestigationResult]]

SUMMARY_NAME = "summary.json"
SNAPSHOT_NAME = "scenarios.json"
REPO_ROOT = Path(__file__).resolve().parents[3]


class RunStatus(StrEnum):
    COMPLETED = "completed"  # the investigator returned a result (it may still have failed)
    ERRORED = "errored"  # the investigator raised
    BUDGET_STOPPED = "budget_stopped"  # cut off by the spend cap mid-run; not a scored attempt


class RunRecord(BaseModel):
    run_id: str
    scenario_id: str
    attempt: int
    status: RunStatus
    log_path: str  # relative to the run directory
    started_at: str
    ended_at: str
    error: str | None = None
    result: InvestigationResult | None = None
    score: RunScore

    @property
    def scored(self) -> bool:
        """Counts toward pass rates."""
        return self.status != RunStatus.BUDGET_STOPPED


class EvalRun(BaseModel):
    run_id: str
    started_at: str
    finished_at: str | None = None
    finished: bool = False  # False in a summary.json means the process died mid-run
    partial: bool = False
    partial_reason: str | None = None
    rescored_at: str | None = None
    runs_per_scenario: int
    concurrency: int
    scenario_difficulty: dict[str, Difficulty] = Field(default_factory=dict)  # planned, in order
    provider: str | None = None
    model_id: str | None = None
    agent_config: dict[str, Any] = Field(default_factory=dict)  # from the first run_start log
    config: dict[str, Any] = Field(default_factory=dict)  # caller-supplied flags
    notes: str = ""
    git_commit: str | None = None
    git_dirty: bool | None = None
    ledger_cost_usd: float | None = (
        None  # spend-ledger total for this run id, if a ledger was given
    )
    records: list[RunRecord] = Field(default_factory=list)
    not_completed: list[str] = Field(default_factory=list)  # "scenario_id/n" never scored

    @property
    def planned_runs(self) -> int:
        return len(self.scenario_difficulty) * self.runs_per_scenario

    @property
    def scored_records(self) -> list[RunRecord]:
        return [r for r in self.records if r.scored]

    @property
    def log_cost_usd(self) -> float:
        """Cost summed from step logs (includes cut-off runs)."""
        return sum(r.score.cost_usd for r in self.records)

    @property
    def total_cost_usd(self) -> float:
        return self.ledger_cost_usd if self.ledger_cost_usd is not None else self.log_cost_usd


# --- helpers -----------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def git_state(cwd: Path | None = None) -> tuple[str | None, bool | None]:
    """(full commit sha, tree has uncommitted changes), or (None, None) outside a git repo."""
    cwd = cwd or (REPO_ROOT if (REPO_ROOT / ".git").exists() else Path.cwd())

    def run(*args: str) -> str | None:
        try:
            out = subprocess.run(
                ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=15, check=True
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout

    sha = run("rev-parse", "HEAD")
    status = run("status", "--porcelain")
    return (sha.strip() if sha else None), (bool(status.strip()) if status is not None else None)


def select_scenarios(
    scenarios: Iterable[ScenarioSpec],
    *,
    ids: Iterable[str] | None = None,
    difficulties: Iterable[Difficulty | str] | None = None,
) -> list[ScenarioSpec]:
    """Subset by scenario id and/or difficulty (both filters must match). Unknown ids raise, so
    a typo does not silently run nothing."""
    pool = list(scenarios)
    if ids is not None:
        wanted = list(ids)
        unknown = sorted(set(wanted) - {s.id for s in pool})
        if unknown:
            raise ValueError(f"unknown scenario id(s): {', '.join(unknown)}")
        pool = [s for s in pool if s.id in set(wanted)]
    if difficulties is not None:
        levels = {Difficulty(d) for d in difficulties}
        pool = [s for s in pool if s.difficulty in levels]
    return pool


def load_eval_run(run_dir: Path) -> EvalRun:
    return EvalRun.model_validate_json((Path(run_dir) / SUMMARY_NAME).read_text(encoding="utf-8"))


def _load_snapshot(run_dir: Path) -> list[ScenarioSpec]:
    raw = json.loads((run_dir / SNAPSHOT_NAME).read_text(encoding="utf-8"))
    return [ScenarioSpec.model_validate(s) for s in raw]


def _record_path(run_dir: Path, scenario_id: str, attempt: int) -> Path:
    return run_dir / scenario_id / f"{attempt}.result.json"


def _score_record(
    spec: ScenarioSpec,
    status: RunStatus,
    result: InvestigationResult | None,
    error: str | None,
    log: StepLog,
    memory: bool = False,
) -> RunScore:
    if result is not None:
        try:
            return score_run(spec, result, log, memory=memory)
        except Exception as exc:  # a scoring bug must never lose a paid-for run
            return score_failed_run(
                spec, log, error=f"scoring failed: {type(exc).__name__}: {exc}", memory=memory
            )
    reason = "spend cap reached mid-run" if status == RunStatus.BUDGET_STOPPED else error
    terminated = "budget" if status == RunStatus.BUDGET_STOPPED else "error"
    return score_failed_run(
        spec, log, error=reason or "unknown error", terminated_reason=terminated, memory=memory
    )


def memory_enabled(config: dict[str, Any]) -> bool:
    """Whether an eval run's recorded config says memory was on (`--memory-dir` / `--memory-seed`
    put `memory_dir` there)."""
    return bool(config.get("memory_dir"))


# --- run ---------------------------------------------------------------------------------


async def run_eval(
    scenarios: Sequence[ScenarioSpec],
    investigator: Investigator,
    *,
    runs_per_scenario: int = 3,
    concurrency: int = 2,
    run_id: str | None = None,
    runs_dir: Path = Path("runs"),
    config: dict[str, Any] | None = None,
    provider: str | None = None,
    model_id: str | None = None,
    notes: str = "",
    ledger: SpendLedger | None = None,
    resume: bool = False,
    retry_errored: bool = False,
) -> EvalRun:
    """Run `investigator` `runs_per_scenario` times per scenario, `concurrency` at a time.

    `provider`/`model_id`/agent config default to what the first `run_start` log reports.
    With a `ledger`, spend is checked before each run (so nothing starts once the cap is hit)
    and `EvalRun.ledger_cost_usd` is the ledger's total for this run's labels. `resume=True`
    continues an existing run directory: finished runs are kept, cut-off runs are redone (their
    old logs are archived), errored runs are redone only with `retry_errored`.
    """
    if runs_per_scenario < 1 or concurrency < 1:
        raise ValueError("runs_per_scenario and concurrency must be >= 1")
    ids = [s.id for s in scenarios]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate scenario ids")

    run_id = run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path(runs_dir) / run_id
    summary_path = run_dir / SUMMARY_NAME
    specs = {s.id: s for s in scenarios}

    if summary_path.exists():
        if not resume:
            raise FileExistsError(f"{summary_path} exists; pass resume=True or choose a new run_id")
        eval_run = load_eval_run(run_dir)
        keep = {RunStatus.COMPLETED} | (set() if retry_errored else {RunStatus.ERRORED})
        eval_run.records = [r for r in eval_run.records if r.status in keep]
        eval_run.finished = eval_run.partial = False
        eval_run.partial_reason = eval_run.finished_at = None
        eval_run.scenario_difficulty |= {s.id: s.difficulty for s in scenarios}
    else:
        commit, dirty = git_state()
        eval_run = EvalRun(
            run_id=run_id,
            started_at=_now(),
            runs_per_scenario=runs_per_scenario,
            concurrency=concurrency,
            scenario_difficulty={s.id: s.difficulty for s in scenarios},
            provider=provider,
            model_id=model_id,
            config=dict(config or {}),
            notes=notes,
            git_commit=commit,
            git_dirty=dirty,
        )
    run_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {s.id: s.model_dump(mode="json") for s in scenarios}
    if (run_dir / SNAPSHOT_NAME).exists():  # keep earlier specs (resume with a subset)
        snapshot = {
            **{s.id: s.model_dump(mode="json") for s in _load_snapshot(run_dir)},
            **snapshot,
        }
    _write_atomic(run_dir / SNAPSHOT_NAME, json.dumps(list(snapshot.values()), indent=2))

    done = {(r.scenario_id, r.attempt) for r in eval_run.records}
    jobs: deque[tuple[ScenarioSpec, int]] = deque(
        (spec, n)
        for n in range(1, runs_per_scenario + 1)
        for spec in scenarios
        if (spec.id, n) not in done
    )
    stop_reason: str | None = None

    def save() -> None:
        eval_run.records.sort(
            key=lambda r: (
                list(specs).index(r.scenario_id) if r.scenario_id in specs else 0,
                r.attempt,
            )
        )
        planned = {(s.id, n) for s in scenarios for n in range(1, runs_per_scenario + 1)}
        scored = {(r.scenario_id, r.attempt) for r in eval_run.records if r.scored}
        eval_run.not_completed = [
            f"{sid}/{n}"
            for sid, n in sorted(planned - scored, key=lambda k: (list(specs).index(k[0]), k[1]))
        ]
        if ledger is not None:
            eval_run.ledger_cost_usd = ledger.summary(label_prefix=f"{run_id}/").spent_usd
        _write_atomic(summary_path, eval_run.model_dump_json(indent=2))

    def archive_old(path: Path) -> None:
        for existing in (path, path.with_name(path.name.removesuffix(".jsonl") + ".result.json")):
            if existing.exists():
                k = 1
                while (
                    target := existing.with_name(f"{existing.stem}.prev{k}{existing.suffix}")
                ).exists():
                    k += 1
                existing.rename(target)

    async def execute(spec: ScenarioSpec, attempt: int) -> None:
        nonlocal stop_reason
        if ledger is not None:
            try:
                ledger.check()
            except BudgetExceededError as exc:
                stop_reason = stop_reason or str(exc)
                return
        rel = Path(spec.id) / f"{attempt}.jsonl"
        log_path = run_dir / rel
        log_path.parent.mkdir(parents=True, exist_ok=True)
        archive_old(log_path)
        started = _now()
        status, result, error = RunStatus.COMPLETED, None, None
        try:
            with spend_label(f"{run_id}/{spec.id}/{attempt}"):
                result = await investigator(spec, attempt, log_path, run_id)
        except BudgetExceededError as exc:
            status, error = RunStatus.BUDGET_STOPPED, str(exc)
            stop_reason = stop_reason or error
        except Exception as exc:
            status, error = RunStatus.ERRORED, f"{type(exc).__name__}: {exc}"
        log = parse_log(log_path, missing_ok=True)
        if status == RunStatus.BUDGET_STOPPED and not log.model_calls:
            return  # never really started: counts as not completed, nothing to keep
        if result is not None:
            error = result.error
        record = RunRecord(
            run_id=run_id,
            scenario_id=spec.id,
            attempt=attempt,
            status=status,
            log_path=rel.as_posix(),
            started_at=started,
            ended_at=_now(),
            error=error,
            result=result,
            score=_score_record(spec, status, result, error, log, memory_enabled(eval_run.config)),
        )
        eval_run.records.append(record)
        if (start := log.run_start) is not None:
            eval_run.provider = eval_run.provider or start.provider
            eval_run.model_id = eval_run.model_id or start.model_id
            eval_run.agent_config = eval_run.agent_config or start.config
        _write_atomic(_record_path(run_dir, spec.id, attempt), record.model_dump_json(indent=2))
        save()
        if ledger is not None and status != RunStatus.BUDGET_STOPPED:
            try:
                ledger.check()
            except BudgetExceededError as exc:
                stop_reason = stop_reason or str(exc)

    async def worker() -> None:
        while jobs and stop_reason is None:
            spec, attempt = jobs.popleft()
            await execute(spec, attempt)

    save()
    completed_normally = False
    try:
        async with anyio.create_task_group() as tg:
            for _ in range(min(concurrency, len(jobs))):
                tg.start_soon(worker)
        completed_normally = True
    finally:
        eval_run.finished = completed_normally
        eval_run.finished_at = _now()
        if stop_reason is not None:
            eval_run.partial, eval_run.partial_reason = True, f"spend cap: {stop_reason}"
        elif not completed_normally:
            eval_run.partial, eval_run.partial_reason = True, "interrupted"
        save()
    return eval_run


# --- rescore -----------------------------------------------------------------------------


def rescore(run_dir: Path, scenarios: Sequence[ScenarioSpec] | None = None) -> EvalRun:
    """Recompute every score in a saved run from its logs and results. No model is called.

    Ground truth comes from `scenarios` if given (use this after fixing a scenario.yaml), else
    from the snapshot saved with the run. Logs and raw results are never modified; scores in
    the per-run result files and `summary.json` are rewritten.
    """
    run_dir = Path(run_dir)
    eval_run = load_eval_run(run_dir)
    specs = {s.id: s for s in (scenarios if scenarios is not None else _load_snapshot(run_dir))}
    for rec in eval_run.records:
        if rec.scenario_id not in specs:
            raise KeyError(f"no scenario spec for {rec.scenario_id}; pass `scenarios`")
        log = parse_log(run_dir / rec.log_path, missing_ok=True)
        rec.score = _score_record(
            specs[rec.scenario_id],
            rec.status,
            rec.result,
            rec.error,
            log,
            memory_enabled(eval_run.config),
        )
        _write_atomic(
            _record_path(run_dir, rec.scenario_id, rec.attempt), rec.model_dump_json(indent=2)
        )
    eval_run.rescored_at = _now()
    _write_atomic(run_dir / SUMMARY_NAME, eval_run.model_dump_json(indent=2))
    return eval_run
