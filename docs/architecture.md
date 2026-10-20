# Architecture

Stub: system architecture and component boundaries. (Sections below are added as each phase lands.)

## Telemetry: wide events

### Schema

The offline backend reads one table, `events`, with **one row per span**. A fixture is
`fixtures/<scenario-id>/events.jsonl.gz` (gzip JSONL, one flat JSON object per line) plus a `scenario.yaml`.
`src/incident_agent/flatten.py` produces it from the OTLP JSON the Collector's file exporter writes.

Fixed columns, present on every row:

| Column | Type | Meaning |
|---|---|---|
| `timestamp` | string | span start, ISO-8601 UTC with microseconds (`2026-10-19T21:00:00.123456Z`) |
| `duration_ms` | float | span duration |
| `trace_id`, `span_id` | string | lowercase hex |
| `parent_span_id` | string or null | null for a root span |
| `name` | string | span name |
| `span.kind` | string | `server`, `client`, `internal`, `producer`, `consumer` |
| `service.name` | string | as the demo emits it |
| `status_code`, `status_message` | string, string or null | `OK` / `ERROR` / `UNSET` and its message |
| `error` | bool | true exactly when `status_code` is `ERROR` |
| `is_root` | bool | `parent_span_id` is null |
| `span_event_count` | int | number of events on the span |

Every resource attribute and every span attribute is then added as its own column with its key unchanged
(`http.route`, `http.response.status_code`, `demo.product.id`, `service.version`, ...). If a resource and a span
attribute share a key, the span's wins. Values are strings, numbers or booleans; arrays and maps become
JSON-encoded strings. From the first span event carrying exception details (the `exception` event, or failing
that the first event with `exception.type` / `exception.message`), `exception.type` and `exception.message` are
lifted onto the row; stack traces are dropped. An attribute a span does not have is simply absent, so in DuckDB the
union of all keys becomes the columns and the rest are null: the table is wide and sparse by design. Row order is
by `timestamp`, and the gzip bytes are reproducible.

### Why flatten

OTLP nests spans under resources and scopes, with attributes as lists of typed key/value pairs. That shape is
right for transport and wrong for asking questions. One flat row per span means:

- **Any field is groupable and filterable** with plain SQL. "Error rate by `demo.product.id`" and "p95 by
  `http.route`" are one-line queries; the agent's tools do not need a per-attribute special case.
- **High-cardinality fields stay.** `trace_id`, `user.id`, `demo.order.id`, `demo.product.id` and free-text
  values are the fields that usually *explain* a problem. They are not pre-aggregated away or dropped to keep
  cardinality down; a `GROUP BY` over one is the tool author's concern (cap and say it was truncated), not the
  schema's.
- **Comparing populations is uniform.** A BubbleUp-style comparison of "failing spans" against "the rest" is the same
  operation over every column, with no join and no schema knowledge. This is the highest-value operation the
  agent has, and it only works because the columns are flat.
- **The model sees the real vocabulary.** Column names are the attribute keys the services actually emit, so
  what the agent reads in a tool result is what an engineer would see in the source or in Honeycomb.

The cost is repetition: resource attributes are copied onto every span of a service. Gzip removes nearly all of
it on disk (the same keys and values repeat), and DuckDB's columnar storage handles the rest in memory.

What a fixture leaves out on purpose: spans from the flag infrastructure (`flagd`, `flagd-ui`, and any span with a
`feature_flag.key` attribute), because they record every flag evaluation with its variant and would simply name the
injected fault; and traces that begin before the recorded window (long-running spans are exported when they end, so
the shared export contains stragglers from earlier).

Not flattened: span links, and event attributes other than the exception fields. Metrics and logs are not
exported to fixtures; traces alone are the dataset. Telemetry text, including attribute values, is untrusted
input and must reach the model as labelled data (see the prompt-injection scenario).

### Sampling

Fixtures are sampled to stay inside the size budget (under 20 MB in total, about 1.5 MB per scenario). Sampling is
**whole-trace and deterministic**: a trace is kept when a hash of its `trace_id` falls in one of N buckets, so
every span of a kept trace is present and re-running gives the same result. The rate is recorded in
`scenario.yaml` as `capture.sample_rate` (keep 1 in N).

Consequences for anything reading a fixture: counts are of **sampled** data, so a raw "1,204 errors" is about
1/N of what really happened; **ratios and percentiles are unaffected** (error rate, p50/p95/p99, shares of a
group), because sampling is uniform over traces. Tools should lean on ratios and say counts are sampled. A rare
event (one trace in a thousand) may be absent; scenarios are designed so the fault produces enough traffic to
survive the chosen rate.

### Capture timeline and ground truth

Each fixture is a healthy baseline period followed by the fault. `capture.fault_start` records the onset, taken
once the feature flag is confirmed live in flagd. It is ground truth for scoring and humans, **never shown to
the agent**. Time windows in the backend are relative to the fixture's own time range. How fixtures are made is in
`infra/README.md`.
