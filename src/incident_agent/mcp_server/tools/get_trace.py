"""get_trace: a summarised trace (condensed span tree, critical path, slowest and error spans)."""

import heapq
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pydantic import Field

from incident_agent.backends.base import Scalar, Span, TelemetryBackend
from incident_agent.mcp_server.tools._common import (
    ToolDef,
    ToolFailure,
    ToolInput,
    clean,
    data_block,
    fmt_num,
    fmt_scalar,
)

NAME = "get_trace"

DESCRIPTION = """\
Summarise ONE request (trace) by its trace_id: a condensed span tree (repeated sibling spans \
collapsed into one line with count/total/max), the critical path (the chain of spans that decided \
the end-to-end time, with self time), the slowest spans by self time, and error spans with their \
status/exception messages and key attributes. The deepest error span with no failing child is \
marked as the likely origin of the failure.
Get trace_ids from find_outliers (example events) - do not invent them. Use it to see how a \
failure or slowdown propagates across services and where it starts. Returns a summary and span_ids \
as pointers, never raw spans; do not use it for aggregate questions (use run_query / find_outliers)."""  # noqa: E501

MAX_SLOWEST = 3
MAX_ERRORS = 4
MAX_ATTRS = 6
MAX_PATH = 10
_EPOCH = datetime.min.replace(tzinfo=UTC)
_NOISE_PREFIXES = (
    "telemetry.", "process.", "host.", "os.", "k8s.", "container.", "cloud.", "otel.",
    "service.instance", "service.namespace", "deployment.", "thread.",
)  # fmt: skip
_NOISE_KEYS = {"status_code", "is_root", "span_event_count", "exception.stacktrace"}
_GOOD_PREFIXES = ("http.", "rpc.", "db.", "messaging.", "app.", "feature", "peer.", "url.", "error")


class GetTraceInput(ToolInput):
    trace_id: str = Field(
        min_length=1,
        max_length=128,
        description="Trace id exactly as shown by find_outliers (hex string).",
    )
    max_tree_lines: int = Field(
        default=25, ge=5, le=60, description="Max lines of the condensed span tree (5-60)."
    )


@dataclass
class Node:
    """Sibling spans with the same (service, name, kind), shown as one line."""

    spans: list[Span]
    children: list["Node"] = field(default_factory=list)

    @property
    def total(self) -> float:
        return sum(s.duration_ms or 0.0 for s in self.spans)

    @property
    def errors(self) -> int:
        return sum(s.error for s in self.spans)

    @property
    def subtree_errors(self) -> int:
        return self.errors + sum(c.subtree_errors for c in self.children)

    def subtree_size(self) -> int:
        return 1 + sum(c.subtree_size() for c in self.children)


def _start(s: Span) -> datetime:
    return s.start or _EPOCH


def _end(s: Span) -> datetime:
    return _start(s) + timedelta(milliseconds=s.duration_ms or 0.0)


def _group(spans: list[Span], kids: dict[str, list[Span]], depth: int = 0) -> list[Node]:
    buckets: dict[tuple, list[Span]] = {}
    for s in sorted(spans, key=lambda s: (_start(s), s.span_id)):
        buckets.setdefault((s.service, s.name, s.kind), []).append(s)
    nodes = []
    for members in buckets.values():
        below = [c for m in members for c in kids.get(m.span_id, [])]
        nodes.append(Node(members, _group(below, kids, depth + 1) if below and depth < 100 else []))
    return nodes


def _self_ms(s: Span, kids: dict[str, list[Span]]) -> float | None:
    if s.start is None or s.duration_ms is None:
        return None
    lo, hi = s.start, _end(s)
    spans = sorted(
        (max(c.start, lo), min(_end(c), hi))
        for c in kids.get(s.span_id, [])
        if c.start is not None and c.duration_ms is not None
    )
    covered, cur_end = timedelta(), lo
    for a, b in spans:
        a = max(a, cur_end)
        if b > a:
            covered += b - a
            cur_end = b
    return max(0.0, s.duration_ms - covered.total_seconds() * 1000)


def _label(s: Span) -> str:
    return f"{clean(s.name or '?', 60)} [{clean(s.service or '?', 40)}]"


def _ms(x: float | None) -> str:
    return "?" if x is None else f"{fmt_num(x)}ms"


def _node_line(n: Node, origins: set[str]) -> str:
    s = n.spans[0]
    if len(n.spans) == 1:
        text = f"{_label(s)} {_ms(s.duration_ms)}"
    else:
        durs = [x.duration_ms or 0.0 for x in n.spans]
        text = (
            f"{_label(s)} x{len(n.spans)} total {_ms(n.total)} avg {_ms(n.total / len(n.spans))} "
            f"max {_ms(max(durs))}"
        )
    if n.errors:
        text += " ERROR" if len(n.spans) == 1 else f" {n.errors} ERROR"
    if any(x.span_id in origins for x in n.spans):
        text += "  <- likely origin"
    return text


def _tree_lines(roots: list[Node], origins: set[str], cap: int) -> list[str]:
    """Pick up to `cap` groups best-first (errors, then time) so ancestors are always shown."""
    chosen: set[int] = set()
    heap: list[tuple[tuple[int, float], int, Node]] = []
    counter = 0

    def push(n: Node) -> None:
        nonlocal counter
        counter += 1
        heapq.heappush(heap, ((-n.subtree_errors, -n.total), counter, n))

    for r in roots:
        push(r)
    while heap and len(chosen) < cap:
        _, _, n = heapq.heappop(heap)
        chosen.add(id(n))
        for c in n.children:
            push(c)

    lines: list[str] = []

    def walk(n: Node, depth: int) -> None:
        lines.append("  " * depth + _node_line(n, origins))
        hidden = [c for c in n.children if id(c) not in chosen]
        for c in n.children:
            if id(c) in chosen:
                walk(c, depth + 1)
        if hidden:
            spans = sum(c.subtree_size() for c in hidden)
            lines.append(
                "  " * (depth + 1)
                + f"... {len(hidden)} more span group(s) hidden ({spans} lines of tree)"
            )

    for r in roots:
        if id(r) in chosen:
            walk(r, 0)
    skipped_roots = [r for r in roots if id(r) not in chosen]
    if skipped_roots:
        lines.append(f"... {len(skipped_roots)} more root group(s) hidden")
    return lines


def _error_below(s: Span, kids: dict[str, list[Span]]) -> bool:
    """Does any descendant of `s` have an error? (Intermediate spans often lack the flag.)"""
    stack = list(kids.get(s.span_id, []))
    while stack:
        c = stack.pop()
        if c.error:
            return True
        stack.extend(kids.get(c.span_id, []))
    return False


def _critical_path(root: Span, kids: dict[str, list[Span]]) -> list[Span]:
    """Spans that decided the end time: the last-finishing child, then (walking back in time) the
    children that finished just before it started, each followed by its own critical children."""
    out = [root]
    pending = list(kids.get(root.span_id, []))
    cursor = _end(root)
    chain: list[Span] = []
    slack = timedelta(milliseconds=1)
    while pending:
        fits = [c for c in pending if _end(c) <= cursor + slack]
        if not fits:
            break
        c = max(fits, key=_end)
        chain.append(c)
        pending.remove(c)
        cursor = _start(c)
    for c in reversed(chain):
        out += _critical_path(c, kids)
        if len(out) >= MAX_PATH:
            break
    return out[:MAX_PATH]


def _key_attrs(s: Span) -> list[tuple[str, Scalar]]:
    def rank(k: str) -> tuple[int, str]:
        if k.startswith("exception."):
            return (0, k)
        return (1 if k.startswith(_GOOD_PREFIXES) else 2, k)

    keys = [k for k in s.attributes if k not in _NOISE_KEYS and not k.startswith(_NOISE_PREFIXES)]
    return [(k, s.attributes[k]) for k in sorted(keys, key=rank)[:MAX_ATTRS]]


def _kv(k: str, v: Scalar) -> str:
    text = fmt_scalar(v, 100 if k == "exception.message" else 60)
    quote = isinstance(v, str) and (" " in v or not v)
    return f"{clean(k, 40)}={json.dumps(text, ensure_ascii=False) if quote else text}"


def _span_block(s: Span, self_ms: float | None, extra: str = "") -> list[str]:
    own = f" self {_ms(self_ms)}" if self_ms is not None and self_ms != s.duration_ms else ""
    out = [f"  {_label(s)} {_ms(s.duration_ms)}{own} span_id={clean(s.span_id, 40)}{extra}"]
    bits = []
    if s.status_message:
        bits.append(
            f"status_message={json.dumps(clean(s.status_message, 100), ensure_ascii=False)}"
        )
    bits += [_kv(k, v) for k, v in _key_attrs(s)]
    if bits:
        out.append("    " + ", ".join(bits))
    return out


def run(backend: TelemetryBackend, args: GetTraceInput) -> str:
    trace_id = args.trace_id.strip()
    trace = backend.get_trace(trace_id)
    if not trace.spans:
        raise ToolFailure(
            f"No spans found for trace_id `{clean(trace_id, 64)}`. Copy a trace_id exactly from "
            "find_outliers' example events (or the `trace_id` field of an event); the trace may "
            "also be absent from this (possibly sampled) dataset."
        )
    spans = trace.spans
    by_id = {s.span_id: s for s in spans}
    kids: dict[str, list[Span]] = defaultdict(list)
    roots: list[Span] = []
    for s in spans:
        if s.parent_span_id and s.parent_span_id in by_id:
            kids[s.parent_span_id].append(s)
        else:
            roots.append(s)
    if not roots:  # cyclic or malformed parent links: fall back to the earliest span
        roots = [min(spans, key=_start)]

    self_times = {s.span_id: _self_ms(s, kids) for s in spans}
    error_spans = [s for s in spans if s.error]
    origins = {s.span_id for s in error_spans if not _error_below(s, kids)}
    top = max(roots, key=lambda s: s.duration_ms or 0.0)
    services = Counter(s.service or "?" for s in spans)
    orphans = sum(1 for r in roots if r.parent_span_id)

    path = _critical_path(top, kids)

    def path_item(s: Span) -> str:
        st = self_times.get(s.span_id)
        return f"{_label(s)} {_ms(s.duration_ms)}" + (
            f" (self {_ms(st)})" if st is not None else ""
        )

    errs = sorted(error_spans, key=lambda s: (s.span_id not in origins, _start(s)))[:MAX_ERRORS]
    listed = {s.span_id for s in errs}  # error spans are described below, not twice
    slowest = sorted(
        (s for s in spans if s.span_id not in listed),
        key=lambda s: -(self_times[s.span_id] or s.duration_ms or 0.0),
    )[:MAX_SLOWEST]

    body = [
        f"services: {', '.join(f'{clean(n, 40)} {c}' for n, c in services.most_common(12))}",
        "span tree (identical siblings collapsed):",
        *("  " + ln for ln in _tree_lines(_group(roots, kids), origins, args.max_tree_lines)),
        "critical path: " + " > ".join(path_item(s) for s in path),
        "slowest spans by self time" + (" (other than the errors below):" if errs else ":"),
    ]
    for s in slowest:
        body += _span_block(s, self_times[s.span_id])
    if errs:
        body.append(f"error spans ({len(error_spans)} total; likely origin first):")
        for s in errs:
            body += _span_block(
                s, self_times[s.span_id], "  <- likely origin" if s.span_id in origins else ""
            )
    else:
        body.append("error spans: none")

    head = (
        f"get_trace {clean(trace_id, 64)}: {trace.span_count} spans, {len(services)} services, "
        f"duration {_ms(top.duration_ms)}, {len(error_spans)} error spans"
    )
    out = [head, data_block(body)]
    notes = []
    if trace.truncated:
        notes.append(
            f"Trace has {trace.span_count} spans; the summary uses the first {len(spans)}."
        )
    if orphans:
        notes.append(
            f"{orphans} span(s) have a parent missing from the data (sampling); shown as roots."
        )
    if len(error_spans) > len(errs):
        notes.append(f"{len(error_spans) - len(errs)} more error spans not listed.")
    return "\n".join([*out, *notes])


TOOL = ToolDef(
    name=NAME,
    title="Summarise a trace",
    description=DESCRIPTION,
    input_model=GetTraceInput,
    run=run,
)
