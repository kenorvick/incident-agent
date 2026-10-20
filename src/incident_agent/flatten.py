"""Flatten OTLP JSON traces into wide events: one flat JSON object per span.

Input is what the OpenTelemetry Collector's `file` exporter writes: one JSON document per line,
each shaped `resourceSpans -> scopeSpans -> spans`. Output is gzip JSONL, one row per span, with
resource and span attributes flattened into columns (span attributes win on collision). See
`docs/architecture.md` for the schema and the reasons behind it.

    uv run python -m incident_agent.flatten raw.jsonl events.jsonl.gz --sample-rate 4

Sampling is whole-trace and deterministic: a trace is kept iff a hash of its trace id falls in
1 of N buckets, so every span of a kept trace is kept and re-running gives identical output.
"""

import argparse
import base64
import binascii
import gzip
import hashlib
import json
import math
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_HEX = re.compile(r"^[0-9a-fA-F]+$")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

_SPAN_KINDS = {
    0: "internal",  # UNSPECIFIED
    1: "internal",
    2: "server",
    3: "client",
    4: "producer",
    5: "consumer",
}
_STATUS_CODES = {0: "UNSET", 1: "OK", 2: "ERROR"}


@dataclass
class FlattenStats:
    """Counts reported after a run. `spans_kept` is the fixture's `capture.span_count`."""

    lines: int = 0
    bad_lines: int = 0
    spans_seen: int = 0
    spans_skipped: int = 0
    traces_seen: int = 0
    traces_kept: int = 0
    spans_kept: int = 0
    sample_rate: int = 1


# --- OTLP JSON decoding helpers -------------------------------------------------------------


def _get(obj: dict[str, Any], *names: str) -> Any:
    """First present key among camelCase / snake_case spellings (both are valid OTLP JSON)."""
    for name in names:
        if name in obj:
            return obj[name]
    return None


def normalize_id(value: Any, hex_len: int) -> str | None:
    """Return a lowercase hex id, accepting hex (OTLP JSON spec) or base64 (plain protojson).

    Empty and all-zero ids mean "absent" and yield None.
    """
    if not isinstance(value, str) or not value:
        return None
    if len(value) == hex_len and _HEX.match(value):
        out = value.lower()
    else:
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            return None
        if len(raw) * 2 != hex_len:
            return None
        out = raw.hex()
    return None if set(out) == {"0"} else out


def _to_int(value: Any) -> int | None:
    """int64 fields arrive as JSON strings (protojson) or numbers."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            try:
                return int(float(value))
            except (ValueError, OverflowError):
                return None
    return None


def iso_timestamp(unix_nano: int) -> str:
    """ISO-8601 UTC with microseconds, e.g. `2026-10-19T21:00:00.123456Z`."""
    dt = _EPOCH + timedelta(microseconds=unix_nano // 1000)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def span_kind(value: Any) -> str:
    if isinstance(value, str):
        name = value.removeprefix("SPAN_KIND_").lower()
        return (
            name
            if name in {"server", "client", "internal", "producer", "consumer"}
            else ("internal")
        )
    number = _to_int(value)
    return _SPAN_KINDS.get(number if number is not None else 0, "internal")


def status_code(value: Any) -> str:
    if isinstance(value, str):
        name = value.removeprefix("STATUS_CODE_").upper()
        return name if name in {"OK", "ERROR", "UNSET"} else "UNSET"
    number = _to_int(value)
    return _STATUS_CODES.get(number if number is not None else 0, "UNSET")


def any_value(value: Any) -> Any:
    """Decode an OTLP AnyValue to a plain Python value (None if empty or unrepresentable)."""
    if not isinstance(value, dict):
        return None
    if "stringValue" in value or "string_value" in value:
        return _get(value, "stringValue", "string_value")
    if "boolValue" in value or "bool_value" in value:
        flag = _get(value, "boolValue", "bool_value")
        return flag.lower() == "true" if isinstance(flag, str) else bool(flag)
    if "intValue" in value or "int_value" in value:
        return _to_int(_get(value, "intValue", "int_value"))
    if "doubleValue" in value or "double_value" in value:
        try:
            number = float(_get(value, "doubleValue", "double_value"))
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else str(number)
    if "arrayValue" in value or "array_value" in value:
        inner = _get(value, "arrayValue", "array_value") or {}
        return [any_value(v) for v in inner.get("values") or []]
    if "kvlistValue" in value or "kvlist_value" in value:
        inner = _get(value, "kvlistValue", "kvlist_value") or {}
        return {
            kv["key"]: any_value(kv.get("value"))
            for kv in inner.get("values") or []
            if isinstance(kv, dict) and "key" in kv
        }
    if "bytesValue" in value or "bytes_value" in value:
        return _get(value, "bytesValue", "bytes_value")  # already base64 text
    return None


def flatten_attributes(attributes: Iterable[dict[str, Any]] | None) -> dict[str, Any]:
    """KeyValue list -> flat dict. Arrays and maps become JSON strings; empty values are dropped."""
    out: dict[str, Any] = {}
    for kv in attributes or []:
        if not isinstance(kv, dict) or not isinstance(kv.get("key"), str):
            continue
        value = any_value(kv.get("value"))
        if value is None:
            continue
        if isinstance(value, list | dict):
            value = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        out[kv["key"]] = value
    return out


def _exception_fields(events: list[dict[str, Any]]) -> dict[str, Any]:
    """`exception.type` / `exception.message` from the first `exception` event.

    Some instrumentation (e.g. the demo's ad service) records the error as an event with another
    name but the same attributes, so fall back to the first event that carries them. Stack traces
    are deliberately dropped: they are large and rarely useful for grouping.
    """
    candidates = [e for e in events if isinstance(e, dict)]
    candidates.sort(key=lambda e: e.get("name") != "exception")  # stable: named ones first
    for event in candidates:
        attrs = flatten_attributes(event.get("attributes"))
        found = {k: attrs[k] for k in ("exception.type", "exception.message") if k in attrs}
        if found:
            return found
    return {}


# --- span -> row ----------------------------------------------------------------------------


def span_to_row(span: dict[str, Any], resource_attrs: dict[str, Any]) -> dict[str, Any] | None:
    """Build one wide-event row, or None if the span has no usable trace/span id or start time."""
    trace_id = normalize_id(_get(span, "traceId", "trace_id"), 32)
    span_id = normalize_id(_get(span, "spanId", "span_id"), 16)
    start = _to_int(_get(span, "startTimeUnixNano", "start_time_unix_nano"))
    if trace_id is None or span_id is None or start is None:
        return None
    end = _to_int(_get(span, "endTimeUnixNano", "end_time_unix_nano")) or start
    parent = normalize_id(_get(span, "parentSpanId", "parent_span_id"), 16)

    status = span.get("status") or {}
    code = status_code(status.get("code"))
    message = status.get("message") or None
    events = span.get("events") or []

    attrs = {**resource_attrs, **flatten_attributes(span.get("attributes"))}
    service = attrs.get("service.name")

    row: dict[str, Any] = {
        "timestamp": iso_timestamp(start),
        "duration_ms": round(max(end - start, 0) / 1e6, 3),
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent,
        "name": span.get("name") or "",
        "span.kind": span_kind(span.get("kind")),
        "service.name": service if isinstance(service, str) and service else "unknown",
        "status_code": code,
        "status_message": message,
        "error": code == "ERROR",
        "is_root": parent is None,
        "span_event_count": len(events),
    }
    for key, value in {**attrs, **_exception_fields(events)}.items():
        row.setdefault(key, value)  # core fields above are never overwritten by attributes
    return row


def iter_spans(document: dict[str, Any]) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """Yield (span, resource_attributes) for every span in one OTLP JSON export request."""
    for resource_spans in _get(document, "resourceSpans", "resource_spans") or []:
        resource = resource_spans.get("resource") or {}
        resource_attrs = flatten_attributes(resource.get("attributes"))
        scope_spans = _get(
            resource_spans, "scopeSpans", "scope_spans", "instrumentationLibrarySpans"
        )
        for scope in scope_spans or []:
            for span in scope.get("spans") or []:
                if isinstance(span, dict):
                    yield span, resource_attrs


# --- sampling -------------------------------------------------------------------------------


def keep_trace(trace_id: str, sample_rate: int) -> bool:
    """Deterministic whole-trace sampling: keep 1 in `sample_rate` traces by hash of the id."""
    if sample_rate <= 1:
        return True
    digest = hashlib.sha256(trace_id.encode("ascii")).digest()
    return int.from_bytes(digest[:8], "big") % sample_rate == 0


# --- file level -----------------------------------------------------------------------------


def _open_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def flatten_documents(
    lines: Iterable[str], sample_rate: int = 1
) -> tuple[list[dict[str, Any]], FlattenStats]:
    """Flatten OTLP JSON lines into rows sorted by (timestamp, span_id); also returns counts."""
    stats = FlattenStats(sample_rate=sample_rate)
    rows: list[dict[str, Any]] = []
    seen_traces: set[str] = set()
    kept_traces: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        stats.lines += 1
        try:
            document = json.loads(line)
        except json.JSONDecodeError:
            stats.bad_lines += 1  # e.g. a line cut in half at a capture boundary
            continue
        if not isinstance(document, dict):
            stats.bad_lines += 1
            continue
        for span, resource_attrs in iter_spans(document):
            stats.spans_seen += 1
            row = span_to_row(span, resource_attrs)
            if row is None:
                stats.spans_skipped += 1
                continue
            seen_traces.add(row["trace_id"])
            if keep_trace(row["trace_id"], sample_rate):
                kept_traces.add(row["trace_id"])
                rows.append(row)
    rows.sort(key=lambda r: (r["timestamp"], r["span_id"]))
    stats.traces_seen = len(seen_traces)
    stats.traces_kept = len(kept_traces)
    stats.spans_kept = len(rows)
    return rows, stats


def write_rows(rows: Iterable[dict[str, Any]], out_path: Path) -> None:
    """Write gzip JSONL atomically (temp + rename) with a fixed gzip header for stable bytes."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    with tmp.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
        for row in rows:
            line = json.dumps(row, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
            gz.write(line.encode("utf-8") + b"\n")
    tmp.replace(out_path)


def flatten_file(raw_path: Path, out_path: Path, sample_rate: int = 1) -> FlattenStats:
    """Flatten a raw OTLP JSONL export (optionally .gz) into a gzip JSONL wide-event file."""
    with _open_text(raw_path) as f:
        rows, stats = flatten_documents(f, sample_rate)
    write_rows(rows, out_path)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m incident_agent.flatten",
        description="Flatten OTLP JSON traces (Collector file exporter output) into wide events.",
    )
    parser.add_argument("raw", type=Path, help="raw OTLP JSONL export (optionally .gz)")
    parser.add_argument("out", type=Path, help="output gzip JSONL, e.g. events.jsonl.gz")
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=1,
        metavar="N",
        help="keep 1 in N traces, chosen deterministically by trace id (default 1 = keep all)",
    )
    args = parser.parse_args(argv)
    if args.sample_rate < 1:
        parser.error("--sample-rate must be >= 1")
    if not args.raw.is_file():
        parser.error(f"raw file not found: {args.raw}")
    stats = flatten_file(args.raw, args.out, args.sample_rate)
    print(json.dumps(asdict(stats)), file=sys.stderr)
    if stats.spans_kept == 0:
        print("warning: no spans written (empty input or sample rate too high?)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
