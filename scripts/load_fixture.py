"""Load a fixture into in-memory DuckDB and print what is in it, to sanity-check a capture.

    uv run python scripts/load_fixture.py fixtures/payment-failure-50pct
    uv run python scripts/load_fixture.py path/to/events.jsonl.gz --top 30

Prints the row count, time range, services (with error counts), and every column with its type and
null fraction. Columns are sparse by design (an attribute missing from a span is simply null).
"""

import argparse
import sys
from pathlib import Path

import duckdb
import yaml


def resolve_events(target: Path) -> Path:
    if target.is_dir():
        for name in ("events.jsonl.gz", "events.jsonl"):
            if (target / name).is_file():
                return target / name
        raise SystemExit(f"no events.jsonl.gz in {target}")
    if target.is_file():
        return target
    raise SystemExit(f"{target} does not exist")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("target", type=Path, help="fixture directory or an events file")
    parser.add_argument("--top", type=int, default=0, help="only show the N least-null columns")
    args = parser.parse_args()

    events = resolve_events(args.target)
    con = duckdb.connect(":memory:")
    # sample_size=-1 reads every row for type inference; rare columns would be missed otherwise.
    con.execute(
        "CREATE TABLE events AS SELECT * FROM read_json_auto(?, format='newline_delimited', "
        "sample_size=-1, maximum_object_size=33554432)",
        [str(events)],
    )

    (rows,) = con.execute("SELECT count(*) FROM events").fetchone()
    print(f"file:    {events} ({events.stat().st_size / 1e6:.2f} MB)")
    print(f"rows:    {rows}")
    if rows == 0:
        print("error: empty fixture", file=sys.stderr)
        return 1

    lo, hi = con.execute("SELECT min(timestamp), max(timestamp) FROM events").fetchone()
    print(f"time:    {lo} .. {hi}")
    (traces,) = con.execute("SELECT count(DISTINCT trace_id) FROM events").fetchone()
    print(f"traces:  {traces}")

    scenario_file = events.parent / "scenario.yaml"
    if scenario_file.is_file():
        capture = yaml.safe_load(scenario_file.read_text()).get("capture", {})
        print(
            f"capture: fault_start={capture.get('fault_start')} flags={capture.get('flags')} "
            f"sample_rate=1/{capture.get('sample_rate')} span_count={capture.get('span_count')}"
        )
        if capture.get("span_count") not in (None, rows):
            print(f"warning: scenario.yaml span_count {capture['span_count']} != {rows} rows")

    print("\nservices:")
    print(f"  {'service.name':26s} {'spans':>8s} {'errors':>8s} {'err%':>6s} {'p95 ms':>9s}")
    for name, spans, errors, p95 in con.execute(
        """SELECT "service.name", count(*), sum(error::INT), quantile_cont(duration_ms, 0.95)
           FROM events GROUP BY 1 ORDER BY 2 DESC"""
    ).fetchall():
        print(f"  {name:26s} {spans:8d} {errors:8d} {100 * errors / spans:6.1f} {p95:9.1f}")

    columns = con.execute("DESCRIBE events").fetchall()
    quoted = ", ".join(f'count("{c[0]}")' for c in columns)
    counts = con.execute(f"SELECT {quoted} FROM events").fetchone()
    table = sorted(
        ((c[0], c[1], 1 - n / rows) for c, n in zip(columns, counts, strict=True)),
        key=lambda t: (t[2], t[0]),
    )
    shown = table[: args.top] if args.top else table
    print(f"\ncolumns ({len(columns)}), least null first:")
    print(f"  {'column':44s} {'type':20s} {'null':>6s}")
    for name, dtype, null_fraction in shown:
        print(f"  {name:44s} {dtype:20s} {100 * null_fraction:5.1f}%")
    if len(shown) < len(table):
        print(f"  ... {len(table) - len(shown)} more (omit --top to see all)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
