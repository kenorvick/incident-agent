"""Python half of scripts/capture_scenario.sh: manifest lookups, slicing, and finalising fixtures.

    capture_helper.py validate-manifest          check infra/scenarios.yaml (schema + flags)
    capture_helper.py list                       scenario ids with difficulty and flags
    capture_helper.py ids                        scenario ids only, one per line
    capture_helper.py show <id> [--shell]        a scenario's capture plan
    capture_helper.py now                        current UTC time, ISO-8601 with microseconds
    capture_helper.py slice SRC START DST [--end N]   copy whole lines of SRC from byte START
    capture_helper.py save-meta <id> ...         record what actually happened during a capture
    capture_helper.py finalize <id> [...]        raw.jsonl -> fixtures/<id>/ events + yaml
    capture_helper.py budget                     total fixture size against the 20 MB budget

Directories default to <repo>/captures and <repo>/fixtures; override with CAPTURES_DIR and
FIXTURES_DIR (used by the tests and for dry runs).
"""

import argparse
import json
import os
import shlex
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import yaml
from scenario_manifest import (
    DEMO_FLAGS_PATH,
    ID_PATTERN,
    REPO_DIR,
    SAMPLE_RATE_LADDER,
    build_spec_dict,
    get_scenario,
    load_manifest,
)

from incident_agent.evals.scenario import ScenarioSpec, load_scenario
from incident_agent.flatten import flatten_documents, keep_trace, write_rows

CAPTURES_DIR = Path(os.environ.get("CAPTURES_DIR", REPO_DIR / "captures"))
FIXTURES_DIR = Path(os.environ.get("FIXTURES_DIR", REPO_DIR / "fixtures"))
DEFAULT_TARGET_MB = 1.4
TOTAL_BUDGET_MB = 20.0


def die(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


# --- manifest -------------------------------------------------------------------------------


def demo_flag_variants() -> dict[str, list[str]] | None:
    """flag -> variant names from the pinned demo clone, or None if it is not cloned."""
    if not DEMO_FLAGS_PATH.exists():
        return None
    flags = json.loads(DEMO_FLAGS_PATH.read_text())["flags"]
    return {name: list(flag.get("variants", {})) for name, flag in flags.items()}


def validate_manifest() -> int:
    manifest = load_manifest()
    problems: list[str] = []
    known = demo_flag_variants()
    if known is None:
        print(f"warning: {DEMO_FLAGS_PATH} not found; flag names/variants not checked")

    seen: set[str] = set()
    counts: dict[str, int] = {}
    for entry in manifest["scenarios"]:
        scenario = get_scenario(manifest, entry["id"])
        sid = scenario["id"]
        if sid in seen:
            problems.append(f"{sid}: duplicate id")
        seen.add(sid)
        if not ID_PATTERN.match(sid):
            problems.append(f"{sid}: id must be lowercase letters, digits and dashes")
        counts[str(scenario["difficulty"])] = counts.get(str(scenario["difficulty"]), 0) + 1

        # The fixture's scenario.yaml must validate; use a placeholder capture block.
        flags = scenario["flags"]
        capture = {
            "demo_version": str(manifest["demo_version"]),
            "flags": flags,
            "baseline_seconds": scenario["baseline_seconds"],
            "fault_seconds": scenario["fault_seconds"],
            "fault_start": None if not flags else "2026-01-01T00:00:00.000000Z",
            "sample_rate": scenario["sample_rate"],
            "span_count": 0,
            "captured_at": "2026-01-01T00:00:00.000000Z",
        }
        try:
            spec = ScenarioSpec.model_validate(build_spec_dict(scenario, capture))
        except Exception as exc:  # pydantic ValidationError: show it once, compactly
            problems.append(f"{sid}: scenario spec invalid: {exc}")
            continue

        healthy = spec.ground_truth.healthy
        if healthy == bool(flags):
            problems.append(f"{sid}: healthy={healthy} but flags={flags}")
        if bool(scenario.get("inject")) != (str(spec.difficulty) == "injection"):
            problems.append(f"{sid}: `inject` must be set for, and only for, injection scenarios")
        cause_flags = {c.flag for c in spec.ground_truth.causes if c.flag}
        herring_flags = set(flags) - cause_flags if not healthy else set()
        if cause_flags - set(flags):
            problems.append(f"{sid}: cause flags {cause_flags - set(flags)} are not enabled")
        if herring_flags and not spec.ground_truth.red_herrings:
            problems.append(f"{sid}: flags {herring_flags} enabled but not a cause or red herring")
        if known is not None:
            for flag, variant in flags.items():
                if flag not in known:
                    version = manifest["demo_version"]
                    problems.append(f"{sid}: flag {flag!r} does not exist in demo {version}")
                elif variant not in known[flag]:
                    options = known[flag]
                    problems.append(f"{sid}: flag {flag!r} has no variant {variant!r} ({options})")

    total = len(seen)
    if not 12 <= total <= 15:
        problems.append(f"expected 12-15 scenarios, found {total}")
    if counts.get("single", 0) < 6:
        problems.append("need at least 6 single-fault scenarios")
    for kind, minimum in (("hard", 2), ("negative", 1), ("injection", 1), ("long", 1)):
        if counts.get(kind, 0) < minimum:
            problems.append(f"need at least {minimum} {kind} scenario(s)")

    print(f"{total} scenarios: {counts}")
    for problem in problems:
        print(f"PROBLEM {problem}")
    if not problems:
        print("manifest OK")
    return 1 if problems else 0


def list_scenarios() -> None:
    manifest = load_manifest()
    for entry in manifest["scenarios"]:
        s = get_scenario(manifest, entry["id"])
        flags = " ".join(f"{k}={v}" for k, v in s["flags"].items()) or "(none)"
        print(f"{s['id']:40s} {s['difficulty']:10s} {flags}")


def show(scenario_id: str, shell: bool) -> None:
    manifest = load_manifest()
    s = get_scenario(manifest, scenario_id)
    plan = {
        "BASELINE_SECONDS": s["baseline_seconds"],
        "FAULT_SECONDS": s["fault_seconds"],
        "USERS": s["users"],
        "SAMPLE_RATE": s["sample_rate"],
        "FLAG_ARGS": " ".join(f"{k}={v}" for k, v in s["flags"].items()),
        "RESTART_BEFORE": " ".join(s.get("restart_before") or []),
        "INJECT": "1" if s.get("inject") else "",
        "DIFFICULTY": s["difficulty"],
    }
    if shell:
        for key, value in plan.items():
            print(f"{key}={shlex.quote(str(value))}")
    else:
        print(yaml.safe_dump({"id": s["id"], "title": s["title"], **plan}, sort_keys=False))


# --- slicing the shared export file ---------------------------------------------------------


def slice_lines(src: Path, start: int, dst: Path, end: int | None = None) -> int:
    """Copy complete lines of `src` that lie within bytes [start, end) into `dst`.

    The collector appends to one file while we record, so the start offset can fall in the middle
    of a line and the end can cut one short; both partial lines are dropped. Returns lines copied.
    """
    size = src.stat().st_size
    end = size if end is None else min(end, size)
    if start > end:
        raise ValueError(f"start offset {start} is beyond end {end}; was the file truncated?")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    lines = 0
    with src.open("rb") as f, tmp.open("wb") as out:
        if start > 0:
            f.seek(start - 1)
            if f.read(1) != b"\n":
                f.readline()  # discard the tail of the line we landed in
        pos = f.tell()
        tail = b""
        while pos < end:
            chunk = f.read(min(8 * 1024 * 1024, end - pos))
            if not chunk:
                break
            pos += len(chunk)
            data = tail + chunk
            cut = data.rfind(b"\n") + 1
            out.write(data[:cut])
            lines += data[:cut].count(b"\n")
            tail = data[cut:]
        # `tail` is an unterminated line at the end boundary: dropped.
    tmp.replace(dst)
    return lines


# --- capture metadata -----------------------------------------------------------------------


def meta_path(scenario_id: str) -> Path:
    return CAPTURES_DIR / scenario_id / "capture.json"


def save_meta(args: argparse.Namespace) -> None:
    manifest = load_manifest()
    s = get_scenario(manifest, args.scenario_id)
    meta = {
        "scenario_id": s["id"],
        "demo_version": str(manifest["demo_version"]),
        "flags": s["flags"],
        "baseline_seconds": args.baseline_seconds,
        "fault_seconds": args.fault_seconds,
        "users": s["users"],
        "fault_start": None if args.fault_start in ("", "none") else args.fault_start,
        "recording_start": args.recording_start,
        "recording_end": args.recording_end,
        "captured_at": args.captured_at,
    }
    path = meta_path(s["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2) + "\n")
    print(f"wrote {path}")


# --- finalising a fixture -------------------------------------------------------------------


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))] if ordered else float("nan")


def signal_table(rows: list[dict[str, Any]], fault_start: str | None, highlight: set[str]) -> str:
    """Per-service error rate and p95 before/after fault onset, to eyeball that the fault shows."""
    if not fault_start:
        return "(no fault onset: healthy control)"
    groups: dict[str, dict[bool, list[dict[str, Any]]]] = {}
    for row in rows:
        groups.setdefault(row["service.name"], {False: [], True: []})[
            row["timestamp"] >= fault_start
        ].append(row)

    def stats(rs: list[dict[str, Any]]) -> tuple[int, float, float]:
        if not rs:
            return 0, float("nan"), float("nan")
        return (
            len(rs),
            100 * sum(r["error"] for r in rs) / len(rs),
            percentile([r["duration_ms"] for r in rs], 0.95),
        )

    lines = [
        f"{'service':22s} {'spans b/a':>14s} {'err% before':>12s} {'err% after':>11s} "
        f"{'p95ms before':>13s} {'p95ms after':>12s}"
    ]
    for service in sorted(groups):
        (nb, eb, pb), (na, ea, pa) = stats(groups[service][False]), stats(groups[service][True])
        mark = "*" if service in highlight else " "
        lines.append(
            f"{mark}{service:21s} {f'{nb}/{na}':>14s} {eb:12.1f} {ea:11.1f} {pb:13.1f} {pa:12.1f}"
        )
    return "\n".join(lines)


def finalize(args: argparse.Namespace) -> int:
    manifest = load_manifest()
    s = get_scenario(manifest, args.scenario_id)
    sid = s["id"]
    raw = Path(args.raw) if args.raw else CAPTURES_DIR / sid / "raw.jsonl"
    if not raw.is_file():
        die(f"{raw} not found. Capture first: scripts/capture_scenario.sh {sid}")
    meta_file = meta_path(sid)
    if not meta_file.is_file():
        die(f"{meta_file} not found (written by capture_scenario.sh); cannot finalise without it")
    meta = json.loads(meta_file.read_text())

    with raw.open(encoding="utf-8") as f:
        rows, stats = flatten_documents(f, sample_rate=1)
    if not rows:
        die(f"no spans parsed from {raw} ({stats.bad_lines} bad lines). Is the export empty?")
    print(f"parsed {stats.spans_seen} spans in {stats.traces_seen} traces from {raw.name}")

    # The export file is shared and long-lived spans are exported when they END, so a capture can
    # contain traces that started before the recording began (a previous scenario's stragglers,
    # multi-minute streaming calls). Keep only traces whose earliest span starts inside the
    # recorded window.
    first_start: dict[str, str] = {}
    for r in rows:  # rows are sorted by timestamp
        first_start.setdefault(r["trace_id"], r["timestamp"])
    lo, hi = meta.get("recording_start"), meta.get("recording_end")
    if lo and hi:
        outside = {t for t, ts in first_start.items() if not lo <= ts <= hi}
        before = len(rows)
        rows = [r for r in rows if r["trace_id"] not in outside]
        print(
            f"dropped {before - len(rows)} spans of {len(outside)} traces that start "
            "outside the recorded window"
        )

    # flagd records every flag evaluation (feature_flag.key / feature_flag.result.variant), and
    # flagd-ui records our own flag writes. Either would hand the agent the injected flag, so the
    # flag infrastructure's spans are left out of fixtures.
    drop = set(s.get("drop_services") or [])
    if drop:
        before = len(rows)
        rows = [r for r in rows if r["service.name"] not in drop and "feature_flag.key" not in r]
        print(
            f"dropped {before - len(rows)} flag-infrastructure spans ({sorted(drop)} and any "
            "span with feature_flag.key): they expose the flags"
        )

    target_mb = args.target_mb or float(s.get("target_mb", DEFAULT_TARGET_MB))
    target = int(target_mb * 1e6)
    out = FIXTURES_DIR / sid / "events.jsonl.gz"
    candidate = out.with_name(".events.candidate.jsonl.gz")  # promoted only if every check passes
    candidates = (
        [args.sample_rate]
        if args.sample_rate
        else [n for n in SAMPLE_RATE_LADDER if n >= int(s["sample_rate"])]
        or [int(s["sample_rate"])]
    )
    chosen = candidates[-1]
    kept: list[dict[str, Any]] = []
    for rate in candidates:
        kept = [r for r in rows if keep_trace(r["trace_id"], rate)]
        write_rows(kept, candidate)
        size = candidate.stat().st_size
        chosen = rate
        print(f"  sample 1/{rate}: {len(kept)} spans, {size / 1e6:.2f} MB")
        if size <= target:
            break
    else:
        print(
            f"warning: {candidate.stat().st_size / 1e6:.2f} MB exceeds the {target_mb} MB "
            f"target even at 1/{chosen}",
            file=sys.stderr,
        )

    def fail(message: str) -> NoReturn:
        candidate.unlink(missing_ok=True)
        die(message)

    services = {r["service.name"] for r in kept}
    causes = s["ground_truth"].get("causes") or []
    herrings = s["ground_truth"].get("red_herrings") or []
    expected = {c["service"] for c in causes} | {h["service"] for h in herrings}
    if s.get("injection"):
        expected.add(s["injection"]["service"])
    missing = sorted(expected - services)
    if missing and not args.allow_missing_service:
        fail(
            f"ground-truth service(s) {missing} are not in the captured data (services seen: "
            f"{sorted(services)}). Fix infra/scenarios.yaml or re-capture; "
            "--allow-missing-service overrides."
        )

    if s.get("injection"):
        field, payload = s["injection"]["field"], s["injection"]["payload"]
        planted = sum(1 for r in kept if r.get(field) == payload)
        if planted == 0:
            fail(
                f"no span in the fixture carries the injection payload in `{field}`. "
                "Planted spans may have been sampled out or never reached the collector."
            )
        print(f"  injection payload present on {planted} spans")

    for flag in meta["flags"]:
        leaks: dict[str, int] = {}
        for r in kept:
            for key, value in r.items():
                if isinstance(value, str) and flag in value:
                    leaks[key] = leaks.get(key, 0) + 1
        if leaks:
            print(
                f"warning: flag name {flag!r} appears in telemetry values: {leaks}", file=sys.stderr
            )

    capture = {
        "demo_version": meta["demo_version"],
        "flags": meta["flags"],
        "baseline_seconds": meta["baseline_seconds"],
        "fault_seconds": meta["fault_seconds"],
        "fault_start": meta["fault_start"],
        "sample_rate": chosen,
        "span_count": len(kept),
        "captured_at": meta["captured_at"],
    }
    spec = ScenarioSpec.model_validate(build_spec_dict(s, capture))
    candidate.replace(out)
    spec_path = FIXTURES_DIR / sid / "scenario.yaml"
    tmp = spec_path.with_name(spec_path.name + ".tmp")
    tmp.write_text(
        yaml.safe_dump(
            spec.model_dump(mode="json", exclude_none=True),
            sort_keys=False,
            allow_unicode=True,
            width=100,
        )
    )
    tmp.replace(spec_path)
    load_scenario(spec_path.parent)  # round trip: validate exactly as the eval loader will

    size_mb = out.stat().st_size / 1e6
    print(f"\nwrote {out} ({size_mb:.2f} MB, {len(kept)} spans, 1/{chosen} traces)")
    print(f"wrote {spec_path} (validated against ScenarioSpec)\n")
    print(signal_table(kept, meta["fault_start"], {c["service"] for c in causes}))
    return budget()


def table() -> None:
    """Markdown rows (id, spans, sample rate, size, window) for the README capture log."""
    for scenario_dir in sorted(FIXTURES_DIR.glob("*/scenario.yaml")):
        spec = load_scenario(scenario_dir.parent)
        size = (scenario_dir.parent / "events.jsonl.gz").stat().st_size / 1e6
        c = spec.capture
        print(
            f"| {spec.id} | {spec.difficulty} | {c.span_count} | 1/{c.sample_rate} | {size:.2f} MB "
            f"| {c.baseline_seconds}s / {c.fault_seconds}s |"
        )


def budget() -> int:
    total = 0
    for events in sorted(FIXTURES_DIR.glob("*/events.jsonl.gz")):
        size = events.stat().st_size
        total += size
        flag = "  <-- over 1.5 MB" if size > DEFAULT_TARGET_MB * 1e6 else ""
        print(f"  {events.parent.name:40s} {size / 1e6:6.2f} MB{flag}")
    print(f"fixtures total: {total / 1e6:.2f} MB of {TOTAL_BUDGET_MB:.0f} MB budget")
    return 1 if total > TOTAL_BUDGET_MB * 1e6 else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate-manifest")
    sub.add_parser("list")
    sub.add_parser("ids")
    sub.add_parser("table")
    sub.add_parser("now")
    sub.add_parser("budget")
    p = sub.add_parser("show")
    p.add_argument("scenario_id")
    p.add_argument("--shell", action="store_true")
    p = sub.add_parser("slice")
    p.add_argument("src", type=Path)
    p.add_argument("start", type=int)
    p.add_argument("dst", type=Path)
    p.add_argument("--end", type=int)
    p = sub.add_parser("save-meta")
    p.add_argument("scenario_id")
    p.add_argument("--baseline-seconds", type=int, required=True)
    p.add_argument("--fault-seconds", type=int, required=True)
    p.add_argument("--fault-start", default="none")
    p.add_argument("--recording-start", required=True)
    p.add_argument("--recording-end", required=True)
    p.add_argument("--captured-at", required=True)
    p = sub.add_parser("finalize")
    p.add_argument("scenario_id")
    p.add_argument("--raw", help="raw OTLP JSONL (default captures/<id>/raw.jsonl)")
    p.add_argument("--sample-rate", type=int, help="force 1-in-N instead of auto-escalating")
    p.add_argument("--target-mb", type=float, help="size target (default: manifest, else 1.4)")
    p.add_argument("--allow-missing-service", action="store_true")
    args = parser.parse_args()

    match args.command:
        case "validate-manifest":
            return validate_manifest()
        case "list":
            list_scenarios()
        case "table":
            table()
        case "ids":
            print("\n".join(s["id"] for s in load_manifest()["scenarios"]))
        case "now":
            print(utc_now())
        case "budget":
            return budget()
        case "show":
            show(args.scenario_id, args.shell)
        case "slice":
            count = slice_lines(args.src, args.start, args.dst, args.end)
            print(f"copied {count} lines to {args.dst}")
        case "save-meta":
            save_meta(args)
        case "finalize":
            return finalize(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
