# Architecture

incident-agent is a small system with one idea at its centre: an agent can only investigate what its
tools let it see, so the tools, the data shape and the measurement matter more than the agent loop.
This document describes what is built, why it is shaped that way, and (in the last two sections) what
would have to change before it served real users. Where something is a design intent rather than
running code, it says so.

Contents: [components and data flow](#components-and-data-flow) ·
[wide events](#telemetry-wide-events) · [record and replay](#record-and-replay) ·
[backend protocol](#the-backend-protocol) · [MCP server](#the-mcp-server) ·
[agent harness](#the-agent-harness) · [evals](#evaluation) · [memory](#memory) ·
[extending memory to user/team/org scopes](#extending-memory-to-per-user--per-team--per-org-scopes) ·
[what would change for production](#what-would-change-for-production)

## Components and data flow

```
 capture time (needs Docker, done once)                    run time (offline, needs only a model key)
 ------------------------------------                      --------------------------------------------
 OTel Demo + flagd fault flag + Locust load
        | OTLP
        v
 OTel Collector --> file exporter --> flatten.py --> fixtures/<id>/events.jsonl.gz + scenario.yaml
        |            (OTLP JSON)     (1 row/span)                         |
        +--> Honeycomb (optional)                                         v
                                                              DuckDBBackend  (in-memory table)
                                                                          |  TelemetryBackend protocol
                          (live, optional) HoneycombBackend --------------+
                                                                          v
                                                              MCP server: 5 tools
                                                                 ^            ^
                                       in-process MCP session    |            |  stdio
                                                                 |            |
                    Agent harness (loop, limits, logs)        external clients (Claude Code, Inspector)
                      ^   model client + spend guard
                      |   opt-in memory tools
                      |
        CLI: investigate        Eval runner --> scoring --> docs/eval-results.md
```

| Component | Code | Role |
|---|---|---|
| Capture pipeline | `infra/`, `scripts/capture_scenario.sh`, `src/incident_agent/flatten.py` | Run the demo with a fault, record traces, flatten to wide events, write fixture and ground truth. Only needed to make new fixtures. |
| Backends | `src/incident_agent/backends/` | `TelemetryBackend` protocol; `DuckDBBackend` (default, offline) and `HoneycombBackend` (optional). |
| MCP server | `src/incident_agent/mcp_server/` | Five read-only, task-shaped tools over a backend. |
| Agent harness | `src/incident_agent/agent/` | Loop, limits, step logs, model clients, spend guard, prompts, opt-in memory. |
| Evals | `src/incident_agent/evals/` | Scenario specs, runner, deterministic scoring, append-only report. |
| CLI | `src/incident_agent/cli.py` | `investigate`, `eval`, `serve-mcp`. |

The agent talks to the MCP server through a real MCP client session. For `investigate` and `eval` that
session runs in-process over the SDK's in-memory transport (`open_session`), with one server and one
backend per investigation, so there is no subprocess to manage and the protocol path is exactly the one
external clients use. `serve-mcp` runs the same server over stdio.

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

## Record and replay

Live fault injection is slow (a capture takes 6 to 14 minutes and the whole demo needs about 5 GB of
images) and non-deterministic, which would make evals flaky and expensive. So:

1. **Record.** For each scenario `scripts/capture_scenario.sh` resets all feature flags, records a healthy
   baseline, switches the scenario's flags on and waits until flagd confirms them (that instant is
   `capture.fault_start`), records the fault period, resets the flags, and slices that window out of the
   Collector's file export. `flatten.py` turns it into a wide-event fixture, and the script writes a
   validated `scenario.yaml` with the alert text and ground truth. The plan for every capture is in
   `infra/scenarios.yaml`; how to run it is in `infra/README.md`.
2. **Replay.** `DuckDBBackend` loads a fixture into an in-memory table and answers every query with SQL.
   Evals run entirely offline and the data never changes between runs, so a score difference is the
   agent's, not the environment's.
3. **Ground truth comes from the flags,** recorded at capture time, never inferred from the agent. The
   onset time and the cause are in `scenario.yaml` and are never shown to the agent. Only the alert text
   is. Alerts are written at the symptom level, like a pager message, and never name the faulty service.

Things done so the replay is an honest test (see `infra/README.md` for the details):

- Spans from `flagd` and `flagd-ui`, and any span with a `feature_flag.key`, are dropped from fixtures:
  they record every flag evaluation with its variant and would simply name the injected fault.
- The prompt-injection scenario plants its instruction-like spans through the real collector, so the
  text reaches the fixture through the same pipeline as everything else.
- Fixtures are sampled to stay inside a 20 MB budget (14 MB total today); see "Sampling" above for what
  that does to counts.

The cost of the approach: the data is a recording of one application at one moment. A tool or prompt can
over-fit to the demo's naming and traffic shape, which is why the scenario set includes hard, negative,
long and adversarial cases and why [the README](../README.md#limitations) says so plainly.

## The backend protocol

`TelemetryBackend` (`backends/base.py`) is the only thing the MCP tools depend on. Every method is
read-only and idempotent:

| Method | Purpose |
|---|---|
| `schema()` | Fields (type, distinct count, presence), row count, time range, services. |
| `aggregate(calcs, filters, group_by, time_window, order_by, limit, granularity_seconds)` | COUNT, COUNT_DISTINCT, AVG, SUM, MIN, MAX, P50/P90/P95/P99 and ERROR_RATE, grouped and/or bucketed over time. |
| `distinct_values(field, ...)` | Most common values with counts, plus the total number of distinct values. |
| `sample_events(...)` | A few raw rows. Not supported by every backend. |
| `get_trace(trace_id)` | All spans of a trace; summarising is the tool's job. |
| `compare_distributions(selection, baseline, ...)` | The BubbleUp primitive: per field, the value distribution in selection vs baseline, scored and ranked. |
| `close()` | Release resources. |

Contract points that the tools rely on:

- **Canonical field names.** The wide-event names (`service.name`, `duration_ms`, `error`, `trace_id`, ...)
  are the vocabulary. Each backend translates to its native names internally.
- **Time is relative to the dataset's own end** (`last_seconds`), not wall-clock, so a fixture captured
  last week answers "the last 5 minutes" sensibly. For Honeycomb, "the end" is now.
- **Truncation is reported** (`total_*`, `truncated`) by every list-returning call.
- **Errors are for the model.** Every bad input raises a `BackendError` subclass (`UnknownFieldError` with
  close matches, `FieldTypeError`, `InvalidQueryError`, `UnsupportedOperationError`,
  `BackendUnavailableError`) whose message is safe and useful to show to the model.
- **Telemetry never becomes SQL.** Column names come from telemetry and contain dots, so every identifier
  is first validated against the loaded schema and then quoted; every value is a bound parameter.

`DuckDBBackend` is the default and the reference implementation. `HoneycombBackend` is optional,
imported lazily so it is never loaded on the default path, selected with `TELEMETRY_BACKEND=honeycomb`,
and built from Honeycomb's Query Data API docs (create query, create query result, poll; plus the
Columns API for the schema). It is honest about what that API cannot do: no raw events (so
`sample_events` raises `UnsupportedOperationError`), a 7-day lookback with coarsened time bounds, no
BubbleUp endpoint (so `compare_distributions` costs about two queries per compared field and only
compares a shortlist per call), and traces rebuilt from grouped queries. It paces itself under the
documented limit of 10 query results per minute. **It has been exercised with mocked HTTP only, never
against a live account** (a live smoke test exists and is skipped without a key), and some assumptions (field-name map, `status_code` encoding) are unverified.

## The MCP server

Five tools, one module each in `mcp_server/tools/`, registered in `TOOLS`: `describe_dataset`,
`run_query`, `find_outliers`, `get_trace`, `compare_windows`. The full contract, each tool's response
shape, the error table, size measurements and known limits are in [tool-design.md](tool-design.md); the
design decisions that matter for the rest of this document are:

- **Task-shaped, not an API wrapper.** One tool per investigative question (what is here, how big and
  where, why, show me one, when did it start). `find_outliers` is the centre of gravity: it is the
  BubbleUp comparison that makes wide, high-cardinality events useful to a model.
- **Context is a budget.** Responses are compact text, typically 150 to 1300 approximate tokens, with an
  explicit truncation line and a "how to narrow" hint. A server-side cap of 12,000 characters
  (`MAX_OUTPUT_CHARS`) catches anything that still gets too big and says so. Tool definitions are also
  slimmed (titles dropped, single-use `$ref`s inlined) because they are re-sent on every model turn.
- **Errors are results.** `TelemetryServer` checks argument names and rewrites validation failures into
  short `isError` results that say what to do next; nothing a tool does can end the session.
- **Read-only by construction.** All tools carry `readOnlyHint`, `idempotentHint`, `destructiveHint=false`
  and `openWorldHint=false`, and the backend protocol has no write method at all.
- **Telemetry is labelled as data.** Anything that came from the dataset appears inside a
  `<telemetry_data>` block under a one-line warning. Values are cleaned: control and bidi characters
  removed, newlines collapsed, a value that would close the block is defanged, long values cut.

Built on the official Python SDK (`mcp` 2.x, where `FastMCP` is now `MCPServer`). Entry points:
`build_server(backend)`, `run_stdio(scenario_id)` and `open_session(scenario_id)`.

## The agent harness

Deliberately small: a loop, limits, a log, a model client, a spend guard and a prompt. Well under
1,000 lines in `agent/` outside memory and the provider clients.

### The loop

`agent/loop.py`. History is a plain, append-only list of provider-neutral messages. Each iteration
checks the limits, calls the model, records the response, and runs every tool call the model requested
concurrently (results keep the order of the calls and go back in one user turn). The run ends when the
model calls `submit_report`, or a limit or error stops it. Nothing in the loop is allowed to crash a run:
tool failures, unknown tools and unparsable arguments come back to the model as error results, and every
way of ending produces an `InvestigationResult` with a report (a synthesized `inconclusive` one when none
was submitted).

The final answer is a structured report (`agent/report.py`) submitted through a harness-local
`submit_report` tool, so scoring never parses prose: a verdict (`root_cause_found`,
`no_significant_issue`, `inconclusive`), causes (service, category, summary), evidence (claim, the
`tool_call_id` that shows it, the values cited exactly as they appear), confidence and uncertainties.
Invalid or inconsistent reports are rejected with the reasons so the model can fix and resubmit.

### Limits and termination

Defaults in `AgentConfig`: 15 model calls, 400,000 input tokens (summed over calls, cached included),
$0.75, 300 seconds wall clock, 16,000 output tokens per response (reasoning included).

| Way a run ends | `terminated_reason` | Report |
|---|---|---|
| Model calls `submit_report` with a valid report | `submitted` | The submitted report. |
| Step limit (15 model calls, nudges included) | `max_steps` | Synthesized `inconclusive`, low confidence. |
| Input-token limit, per-investigation cost limit, or wall-clock limit | `budget` | Synthesized `inconclusive`. |
| Global spend cap reached (`BudgetExceededError`) | `budget` | Synthesized `inconclusive`. No API call was made. |
| Model ends its turn without submitting, after one reminder | `error` | Synthesized `inconclusive`. |
| Response cut off by the output limit three times in a row | `error` | Synthesized `inconclusive`. (Each cutoff gets a "be more concise" nudge first.) |
| Any other exception (provider failure after retries, auth error, bug) | `error` | Synthesized `inconclusive`, with the error text. |

A synthesized report says explicitly that it "says nothing about whether the system is healthy", so an
inconclusive run is never read as a negative result. If a report was submitted just before a limit fired,
it still counts.

### Step logs

`agent/tracing.py` writes one JSON object per line, flushed after every event, so a crash keeps
everything so far. The format is a contract with the eval harness (`evals/logs.py` reads it):

| Event | Fields |
|---|---|
| `run_start` | run id, scenario id, alert, provider, model id, config |
| `model_call` | step, uncached input / output / cache-read / cache-write tokens, cost, latency, stop reason, the model's text, its tool calls (id, name, arguments) |
| `tool_result` | step, tool call id, name, arguments, `is_error`, size in chars and approximate tokens, latency, `truncated_by_harness`, and the exact `content` the model saw |
| `context_event` | a `truncate` / `clear` / `compact` record with details (see [Context management](#context-management)). Emitted only when a mechanism acts. A compaction summary call is also a `model_call`, marked `purpose: "compaction"`. |
| `final` | the report and the termination reason |
| `run_end` | steps, tool calls, tokens, cost, duration |

Logging the exact tool result text is what makes the anti-fabrication check possible: a cited value is
grounded only if it occurs in the result of the tool call it cites.

### Model clients and the spend guard

`agent/models/`. A provider-neutral `ModelClient` protocol (`complete(system, messages, tools,
max_tokens)`) with two implementations: **OpenRouter** (default; chat completions over httpx) and the
**Anthropic API** (official SDK). `get_model_client()` is the only way to build one and it always returns
the client wrapped by `BudgetedModelClient`. This is a deliberate deviation from PLAN.md, which named
Amazon Bedrock as the first provider: the repo owner chose OpenRouter instead, and the protocol makes
adding Bedrock a single module.

- **Spend guard.** Before every call the wrapper reads the append-only ledger
  (`.cache/spend-ledger.jsonl`, gitignored) and refuses to call the provider if cumulative spend has
  reached `SPEND_CAP_USD`; after the call it appends one entry (tokens by kind, cost, a
  `run/scenario/attempt` label). The cap survives restarts and is shared by processes. Concurrent calls
  can overshoot by the cost of those already in flight. The project's total budget is $10.
- **Cost accounting.** OpenRouter returns its own cost per call, which wins; for the Anthropic client,
  or when a cost is missing, cost is estimated from a price table, and models not in the table are priced
  at a deliberately high rate so the guard errs safe. Each entry records whether the cost was reported
  by the provider or estimated.
- **Retries.** Up to 5 attempts with exponential backoff, jitter and `Retry-After` support on
  408/409/425/429/5xx-class statuses and transport errors; auth and bad-request errors are not retried.
  Errors carry messages written to be acted on (for example, where to add credits).
- **Reasoning.** Sonnet 5.5 always reasons (billed as output); `MODEL_EFFORT` (default `medium`) caps it,
  and reasoning blocks are echoed back on tool-use turns as the providers require.

### Prompt caching and the stable prefix

Every model turn re-sends the tool definitions, the system prompt and the whole history, so the cost of
a long investigation is dominated by re-reading a prefix that does not change. The harness is arranged so
that prefix is byte-stable and cacheable:

- The **system prompt is static** (`agent/prompts/system.md`, cached in-process). It contains nothing
  per-run: the alert goes in the first user message.
- The **tool list is fixed**: the five MCP tools in a fixed order, then `submit_report`. Schema titles
  are stripped deterministically, and the optional per-dataset field-name enum
  (`dynamic_field_enum`) is off by default, partly because it would change the definitions per dataset
  and bloat them.
- History is **append-only by default**: no message is rewritten, so each turn's prefix is the previous
  turn's prefix plus new content. The opt-in context mechanisms (below) break that on purpose, at a cost
  in cache hits that is described in [Context management](#context-management).
- On OpenRouter the client marks the system block and sets top-level automatic cache control, which
  advances to the last block of the conversation on each call (two of Anthropic's four breakpoints). The
  prefix order is tools, then system, then messages, so the system breakpoint covers the tools too. On the
  Anthropic path, breakpoints go on the last tool, the system prompt, and the last block of the two most
  recent user turns. Cache reads and writes are logged and priced separately.
- Memory-enabled runs append a memory section to the system prompt and add two tools. That is a different
  prefix from memory-off runs, but it is still stable within a run.

In the first live investigation (`payment-failure-50pct`), cache reads were visible in the step log and
the run, 3 steps and 4 tool calls to the right service, cost about $0.04.

### Prompt

The system prompt (`agent/prompts/system.md`) states the role and the method (orient, quantify, narrow,
compare, drill into a trace, compare windows, conclude after checking alternatives), the evidence rule
(every claim cites a tool call and copies values exactly), the untrusted-data rule, the
"when there is no problem" rule that makes `no_significant_issue` an acceptable and expected answer, and
how to finish. The prompt is a tuning surface, so changes to it are made one at a time and measured; see
[lessons-learned.md](lessons-learned.md).

### Context management

`agent/context.py`, wired into the loop before every model call. **Read this first: no run has ever
needed it.** In every eval so far an investigation takes 4 to 6 model calls, the largest single request is
about 16k tokens (of which the system prompt and tool definitions are about 8.2k), and cumulative input is
at most about 60k. So the mechanisms are built, tested with scripted models, and meant to be *evaluated
under a deliberately constrained window* that simulates a long investigation or a small-context model.
They are not tuned to a measured failure, and any claim that they improve accuracy has to come from that
simulated comparison, not from production-like runs. No such comparison has been run yet (it costs
real model spend), so there are no accuracy numbers for these mechanisms in this repo.

Settings (`AgentConfig.context`, a `ContextConfig`; CLI `--context-mode` and `--max-context-tokens`):

| Setting | Default | Meaning |
|---|---|---|
| `max_tool_result_tokens` | 4000 (on) | Hard cap on one tool result (about 4 characters per token). Cheap safety net. |
| `max_context_tokens` | none (unlimited) | The per-call window the run must fit in. Required for `clear` and `compact`. |
| `mode` | `none` | `none`, `clear` or `compact`. |
| `clear_threshold` / `compact_threshold` | 0.6 / 0.75 | Share of the window at which clearing / compaction starts. |
| `keep_recent` | 2 | Tool-result turns left intact when clearing. |
| `max_compactions`, `compact_min_history_tokens` | 3, 3000 | Compaction budget per run; the least history worth summarizing. |

How a request is sized: the provider's reported total input tokens of the last call, plus the
approximate tokens (characters / 4) of the messages appended since. The first request is the prefix plus
the alert, estimated from characters (JSON schemas at 2.5 characters per token; 7.8k estimated against
8.2k reported). Everything else is an estimate, so the window is a simulation and not a hard provider
limit.

- **Cap.** A result over the cap is cut at a line boundary and ends with a note: what was cut (characters
  and tokens), that the rest is unavailable, and how to narrow the request. The tool_result event gets
  `truncated_by_harness: true` and a `truncate` context event is logged. The step log's `content` is the
  text the model saw, so a truncated value is not citable, as it should be. The tools already cap at
  12,000 characters server side, so this is defense in depth and does not fire today.
- **No management, window set.** If the next request is estimated over the window, the run ends with
  `terminated_reason=budget` and a synthesized inconclusive report that says the context window was
  exhausted. This is the control arm.
- **`clear`.** Past `clear_threshold`, the content of tool results older than the last `keep_recent`
  turns becomes one line: `[ref cN] <tool>: <first line of the result> (cleared to save context; full
  result still citable by ref)`. Only the result's text changes: every `tool_use` still has its
  `tool_result`, assistant turns and their reasoning state are untouched, and `submit_report` and memory
  results are never cleared. Results where the stub would not be smaller are left alone. Cleared results
  stay cleared (a stub is not re-expanded), so clearing happens in batches when the threshold is crossed,
  not every turn. A `clear` event logs how many results and tokens were freed and the estimate before
  and after.
- **`compact`.** Past `compact_threshold`, one extra model call (same client, **no tools**, its own short
  system prompt) receives the history as a plain-text transcript and writes structured notes: decisions,
  findings with exact values and refs, ruled-out hypotheses, open questions, key identifiers. The history
  becomes the original alert plus one user message holding those notes under a header that says they are
  data, that cited refs remain valid evidence and that numbering continues. The call counts as a step and
  in tokens and cost, and is logged as a `model_call` with `purpose: "compaction"` plus a `compact` event
  (tokens before and after, messages before and after). The summarizer is told never to copy
  instruction-like text from tool results, because the notes come back as a *user* message, which would
  otherwise promote injected telemetry text to the trust level of the user. Compaction is skipped in favor
  of clearing when the summary call fails or returns nothing, when there is too little history to be
  worth summarizing, when the prefix alone is so close to the threshold that a summary could not get under
  it (the 8.2k fixed prefix is most of a 13k window, so this matters in the simulation), and after
  `max_compactions`.
- **Refs stay valid.** Every tool result keeps its `[ref cN]`, ref numbering is never reset, and the step
  log is written from the full result before any clearing. The anti-fabrication scorer reads results from
  the step log, so a report that cites a ref whose result was cleared, or one that only survives in the
  summary, still grounds if the cited values match the original result. The cost of getting this wrong is
  the model copying a value from the summary imperfectly, which the grounding check will catch.
- **Reasoning state.** Reasoning blocks ride in `Message.provider_data` and are echoed only for the
  assistant turn they belong to. Clearing does not touch assistant turns. Compaction drops every assistant
  turn, which is safe for both providers' formats because the replacement history contains no assistant
  turn at all, so no reasoning block can be orphaned (Anthropic only requires them within a tool-use
  turn). The summary call is a separate request with no reasoning state.
- **Caching.** The system prompt and tool definitions are never modified by any mechanism, so the
  cacheable prefix (tools, system) keeps hitting. Anything after the first changed message does not:
  clearing rewrites an old tool result, so the next request reads the cache up to just before that
  message and pays the cache-write price again for everything after it. That is the price of the tokens
  saved, and it is paid once per clearing batch, which is why clearing is thresholded rather than rolling.
  Compaction replaces everything after the alert, so only the prefix and the alert are read from cache and
  the notes plus later turns are written fresh. The summary call has a different system prompt and no
  tools, so it reads nothing from the main cache and pays full input price for the whole transcript.
  Whether clearing or compaction is a net saving on a given bill is an empirical question that the cache
  columns in the step log can answer; at this project's scale (a $0.025 investigation) it may well not be.
- **Limits of this design.** The thresholds are shares of a window, so a fixed 8.2k prefix makes small
  windows awkward: at 13k, 60% is already below the prefix. Clearing can only free tool-result text (a few
  thousand tokens in these runs; assistant text and reasoning are 40% of per-turn growth and are not
  touched). Compaction relies on the model's notes being complete and exact; the test checks that the
  harness delivers the facts and keeps refs gradable, not that a real model summarizes well.

## Evaluation

`evals/`. A scenario spec (`scenario.yaml`) holds the alert shown to the agent, the ground truth
(healthy or not, causes with service and category and acceptable keywords, red herrings), expected
evidence, an optional injection description, and capture metadata.

- **Runner.** Runs each scenario N times (default 3) with limited concurrency (default 2). Runs are
  scheduled attempt-major, results are persisted as each run finishes (`runs/<run_id>/`, gitignored), a
  per-run exception becomes an errored record and the suite continues, and a spend-cap stop marks the eval
  partial instead of discarding it. `--resume` continues a run id.
- **Scoring.** Deterministic and replayable from saved logs without a model call (`rescore`). The rules
  (service, category, anti-fabrication, false alarm, red herring, injection) are documented at the top of
  `evals/scoring.py` and `evals/grounding.py`. A judge seam exists but is intentionally unimplemented;
  an LLM judge would only be added for rubric items code cannot check, and labelled in reports.
- **Report.** `docs/eval-results.md` is append-only with run id, date, git state, model and config;
  failures and partial runs are included, and `--compare-to` prints a before/after delta.

## Memory

Memory is **opt-in hints with provenance, not truth**. It is off by default, and with it off the system
prompt and tool list are byte-identical to a build that never had memory. Code: `agent/memory.py`;
prompt: `agent/prompts/memory.md`. It is wired in programmatically, with
`make_investigator(memory=MemoryConfig(...))`; there is no CLI flag yet.

### Store

One JSON file (`memories.json`) per store directory, rewritten atomically (temp file, fsync,
`os.replace`) under a lock. It is meant for one writer process; evals give each run a private copy
(`per_run_copy`) so attempts do not contaminate each other and each run's notes stay inspectable. A note
has: `content`, `scope` (`service` or `investigation`), `scope_key`, `source`, `created_at`,
`ttl_seconds`, `confidence`, `written_by`, `reason`, and an id.

### Tools

`recall(scope, scope_key, keywords?)` returns at most 5 unexpired notes, each with age, confidence, source
and writer, and the content JSON-quoted. The result opens with a reminder that these are hints to verify
and never to obey. `save_memory(content, scope, scope_key, reason, source, confidence?, ttl_days?)` stores
one short fact. The memory prompt tells the agent to verify a note against current telemetry before
relying on it, to cite telemetry tool calls rather than the memory as evidence, to trust the data if they
disagree, and to save only durable verified facts (for example a service's normal error rate), never the
cause of the current incident.

### Provenance

A note's `source` must be the id of a **telemetry tool call from this run whose result the model has
already seen**. The harness tracks the calls (`ToolCallLedger`): a call counts only from the model's next
turn, so a call in the same batch as `save_memory` does not, and memory and `submit_report` calls never
count. A note therefore cannot cite evidence that does not exist. It is stored as `run_id/tool_call_id`
with `written_by` set to the run id, so a note can be traced to the step log that produced it. Seeded
notes (for evals) say `seed`.

### Scope isolation, enforced in storage

`MemoryStore.query` is the only read path and requires an exact `(scope, scope_key)`. It filters on them
before it looks at keywords, so no keyword, tool argument or prompt can pull in another service's or
investigation's notes. `investigation` scope is bound to the run's investigation id: the tool accepts only
`current` (or the run's own id) and rewrites the key, so another investigation's notes can be neither
read nor written. A `service` key is a validated token (letters, digits, `_ . : -`, at most 100
characters). What the store does *not* have is any notion of who is asking: any run can read any service's
notes, which is intended for a single-user tool and is exactly the gap the next section closes.

### Expiry

Default TTL 7 days, clamped to 1 to 30. Expired notes are never returned by `query`, and are purged on the
next write. At most 50 active notes per `(scope, scope_key)`, at most 5 saves per investigation, and an
identical active note is not duplicated.

### Sanitization

`sanitize_content` runs on every write, including seeds. It refuses content that looks like a telemetry
dump (starts like JSON, many JSON-style fields or `key=value` pairs, stack traces, 32-hex trace ids) or
like an instruction (overrides of earlier instructions, "new instructions", "from now on", text
addressed to the agent, conditional commands, role and chat markers, a command at the start of a
sentence, mentions of the harness's own tools). It NFKC-normalizes, strips control and format
characters (zero-width, bidi), enforces a 400-character limit (refusing rather than truncating), and
collapses to one line. Refusals are written for the model ("rewrite it as a plain factual statement").

This is a lexical heuristic on English, and the code says so: paraphrase, other languages, leetspeak,
split phrases and instructions phrased as facts get through, and plain factual notes that begin with a
verb are falsely rejected. It is one layer among several: only the agent can write notes, each needs a
real source call, recalled text is labelled unverified and quoted as data, the prompt requires
verification, notes expire, and retrieval is scoped.

Status: the store, tools, sanitizer, provenance and isolation are unit-tested (`tests/agent/test_memory*.py`).
The PLAN's memory evals (a benefit pair, a seeded stale-memory harm case, an isolation eval) are not yet
built; seeding support (`MemoryConfig.seed_file`, relative `age_days`/`ttl_days`) exists for them.

## Extending memory to per-user / per-team / per-org scopes

The current store is single-tenant by construction: one file, two scopes, no identity. A multi-tenant
version keeps the same ideas (hints with provenance, expiry, scoped retrieval, sanitization on write) and
moves enforcement out of conventions and into the storage layer, where a bug in the agent, the prompt or
a tool cannot widen access. This section is a design; none of it is implemented.

### Scope hierarchy

```
org                       one per customer account (the tenant boundary)
 └─ team                  a group inside the org; many per org
     └─ user              a person; may belong to several teams
         └─ investigation one incident / session; private to the user who ran it
```

`service` (what a note is about) stays a separate dimension from *who may see it*. A note is addressed
by `(tenant_id, level, owner_id, subject_key)`: for example `(acme, team, sre-payments, service:payment)`.
`org` is the tenant: nothing is ever visible across orgs, at any level, for any role.

Reading resolves **down** the hierarchy for the caller: a user sees their own notes, their
investigation's notes, their teams' notes and their org's notes, in that precedence order, each labelled
with its level. Narrower levels never see wider levels' *private* notes, and wider levels never see
narrower ones' unless promoted (below).

### Tenancy enforced in the storage layer

- **Tenant id in every key and row.** Every table and index is keyed with `tenant_id` first (primary
  key `(tenant_id, id)`, indexes `(tenant_id, level, owner_id, subject_key)`). Cache keys and any search
  index include it too.
- **Row-level security, not application filters.** In Postgres, `ALTER TABLE memory_notes ENABLE ROW LEVEL
  SECURITY` with a policy of `tenant_id = current_setting('app.tenant_id')::uuid` (plus a level/owner
  check for non-org rows). The service sets `app.tenant_id` and `app.user_id` per transaction from the
  **verified access token**, connects as a role without `BYPASSRLS`, and has no superuser path in the
  request handler. A forgotten `WHERE` then returns nothing instead of everything. Where the customer
  or compliance needs stronger separation, use per-tenant partitions, schemas or databases (and
  per-tenant encryption keys) behind the same interface.
- **No cross-tenant query path.** The repository interface has no method that does not take a
  `TenantContext`, and the context cannot be constructed from model-supplied arguments: the model may
  choose a *subject* (`service:payment`) and a *level* from an enum, never a tenant, user or team. A test
  suite asserts that, for every repository method, a context for tenant A can never read, write, update,
  or delete tenant B's rows, including via keyword search, pagination cursors and error messages.
- **No shared infrastructure that blurs it.** Per-tenant namespaces for any queue, cache or index; no
  global "recent notes" feeds.

### Authorization on read and write

Authentication gives `(tenant, user, team memberships, roles)` from the token. Authorization is checked in
the service layer on every operation, with RLS as the backstop:

| Operation | Allowed when |
|---|---|
| Read investigation / user note | The caller owns it (investigation members may read their investigation). |
| Read team note | The caller is a member of that team. |
| Read org note | The caller belongs to the org. |
| Write investigation / user note | The caller is acting (the agent acts *as* the user, with the user's scopes, never a service identity that can write anywhere). |
| Write team note | Team member, directly, only if the team policy allows direct writes; otherwise by promotion. |
| Write org note | Org admin, or promotion only. |
| Delete / edit | Author, or team/org admin for their scope. |

The agent can only create notes at **investigation or user** level. It cannot write directly to team or
org scope, which keeps a manipulated agent from poisoning shared memory. Reads of notes that were
derived from data a caller is not allowed to see need care: a note carries the data classification and
dataset of its source, and is withheld from callers who lack access to that dataset.

### Provenance and audit

- Each note keeps what the current one has (source tool call, writer run, reason, confidence) plus the
  actor (user id), the agent version and model id, the dataset it was derived from, and a pointer into a
  **retained step log** so the evidence behind a note can be inspected, not just claimed.
- Every read, write, edit, promotion, demotion and delete appends to an **audit log**: tenant, actor
  (user and agent run), operation, note ids, scope level, outcome, timestamp. It is append-only, separate
  from application logs, and queryable per tenant ("who saw or changed this note?").
- Recall results always label level, age, source and confidence, so the model (and a human reading the
  log) can tell an org-wide convention from a note one engineer saved last Tuesday.

### Retention and TTL per scope

| Level | Default TTL | Maximum | Notes |
|---|---|---|---|
| investigation | the life of the investigation, then days | 7 days | Cleared when the investigation closes. |
| user | 30 days | 90 days | Deleted on user offboarding or erasure request. |
| team | 90 days | 180 days | Re-confirmation extends it; unconfirmed notes lapse. |
| org | 180 days | 365 days | Needs periodic review by an owner. |

Expiry is honored on read (as today) and swept in the background; policies are per tenant and configurable
down, never above the maximum. Legal holds and erasure requests are explicit operations that propagate to
derived notes through provenance.

### Promotion: user → team → org

Facts that stay useful should move up, but only through a gate:

1. A user (or the agent, on the user's behalf) **proposes** promotion of a note. This creates a pending
   proposal; it does not copy the note anywhere.
2. The proposal is re-run through sanitization and checked for sensitive content and dataset
   classification, then shown to a **human reviewer** with the note, its source evidence (linked step log)
   and its history. The agent can never approve its own proposal.
3. On approval the promoted note is created at the wider level with the original provenance, plus the
   proposer, reviewer and decision time. Its confidence resets to low until it is re-verified in use, and
   its TTL follows the wider level's policy.
4. Reviewers can demote or revoke later, and a revocation removes the wider copy and its audit-visible
   effect. Contradicted notes (the data disagreed with the note during an investigation) are flagged for
   review rather than silently overwritten.

### How the current file store maps onto this

| Today | Multi-tenant |
|---|---|
| `memories.json` in a directory | A `memory_notes` table with `tenant_id`, under RLS (a directory per tenant would be the minimal file-based step). |
| `Scope.SERVICE`, `Scope.INVESTIGATION` | Levels plus a subject key: `service:<name>` at team/org/user level, `investigation` as a level. |
| `scope_key` | `owner_id` (user, team, org, investigation) plus `subject_key`. |
| `MemoryStore.query` as the only read path | A repository whose every method takes a `TenantContext`; `query` becomes "the visible notes for this context". |
| Exact `(scope, scope_key)` match before keywords | The same ordering, with the tenant and level predicate enforced by the database. |
| `written_by` (run id) | Actor user id, agent run id, agent version, model. |
| `source` (`run_id/tool_call_id`, must exist in this run) | Same check, plus a durable link to the retained step log and dataset. |
| `ttl_seconds`, clamp to 30 days, 50-per-key cap | Per-level policy table (above) and per-tenant quotas. |
| `sanitize_content` | Unchanged, run on write and again on promotion; supplemented by review. |
| Atomic file replace under a lock | Database transactions. |
| Single-process, per-run copies for evals | Per-tenant stores; evals keep using isolated per-run copies. |

## What would change for production

This repository is a prototype of the agent, the tools and the measurement loop. The things below are
what a hosted, multi-tenant service handling other people's telemetry would need. None of them is built;
PLAN.md put auth and multi-tenant hardening explicitly out of scope and asked that they be documented.

### Authorization for the MCP server (OAuth 2.1)

Today the server runs over stdio, trusts its local caller, and reads local fixtures. For a hosted server
over HTTP the MCP authorization specification applies. The terms below were checked against the
specification revision **2026-07-28** (modelcontextprotocol.io) on 2026-10-20; re-check them against the
current revision before building, because this area has changed between revisions.

- **Roles.** The MCP server is an OAuth 2.1 **resource server** (the spec cites the OAuth 2.1 IETF draft,
  `draft-ietf-oauth-v2-1-13`); the MCP client is an OAuth client; a separate **authorization server**
  issues tokens. Authorization is optional in the spec, and for HTTP transports implementations SHOULD
  follow it. For **stdio the spec says implementations SHOULD NOT** follow it and should take credentials
  from the environment, which is what this repo does today.
- **Discovery.** The server MUST implement OAuth 2.0 Protected Resource Metadata (RFC 9728) and answer an
  unauthenticated request with `401` and a `WWW-Authenticate` header that carries `resource_metadata`;
  clients discover the authorization server from it, and the authorization server must expose RFC 8414
  metadata or OpenID Connect discovery.
- **Token audience binding.** Clients MUST send the `resource` parameter (RFC 8707, Resource Indicators)
  in both the authorization request and the token request, naming the MCP server's canonical URI, so the
  token is minted **for this server only**. The server MUST validate that every access token was issued
  specifically for it as the audience, and MUST reject tokens that do not name it. Validation follows
  OAuth 2.1 section 5.2 (signature, issuer, expiry, audience); invalid or expired tokens get `401`, and
  insufficient scope gets `403` with `error="insufficient_scope"` and the scopes needed.
- **No token passthrough.** The server MUST NOT accept or pass on tokens issued for any other resource. If
  it calls an upstream API (here Honeycomb), that is a **separate credential obtained for the upstream**,
  never the token the MCP client presented. The incoming token authenticates the user to our server only;
  the server then uses its own per-tenant upstream credential, chosen by the verified tenant claim.
- **Clients and flows.** PKCE with `S256` is required of clients (and they must refuse authorization
  servers that do not advertise support). Client ID Metadata Documents are the preferred client
  registration mechanism (SHOULD), pre-registration is allowed, and Dynamic Client Registration is
  retained but marked deprecated. Redirect URIs must be exact-match registered; access tokens go only in
  the `Authorization: Bearer` header, never the query string, on every request; short-lived access tokens
  are recommended.
- **Scopes.** Map scopes to what the tools do: one read scope per dataset class (for example
  `telemetry:read`), and a separate scope for memory writes. The challenge advertises the scopes needed
  for the operation; clients step up when challenged. All the current tools are read-only, which keeps
  the scope surface small; any future write tool gets its own scope and a confirmation step.

### Per-tenant authorization at the data layer

Authentication at the front door is not enough: the check that matters is at the data. The verified token
yields `(tenant, user, roles)`; those are put into a request context that the **backend** requires. For a
Honeycomb backend that means a per-tenant key (and dataset or environment) with the minimum permissions
the Query Data API needs (Run Queries plus Manage Queries and Columns), resolved from the tenant id, so a
query for tenant A literally cannot be sent with tenant B's key. For a warehouse-style backend it means
row-level security keyed on tenant, as in the memory design above. The model never supplies a tenant,
dataset or credential. Tool arguments are still validated against the tenant's own schema, so a field
name from another tenant's data is "not found" with no information leaked in the suggestion list.

### Rate limiting and quotas

- **Per tenant and per tool**, token-bucket limits at the MCP edge with `429` and `Retry-After`, plus a
  cap on concurrent calls per session so a runaway agent loop cannot saturate a tenant's backend.
- **Backend query budgets.** The Honeycomb Query Data API allows about 10 query results per minute
  (fewer for relational queries) per key, and `find_outliers` costs roughly two queries per compared
  field there. The current backend paces itself in one process; production needs a **shared** limiter
  across replicas, per-tenant allocation of the key's budget, cost-aware accounting (a tool declares how
  many backend queries it will issue), and caching of identical idempotent queries (the tools are
  idempotent, so results can be cached briefly keyed on tenant and arguments).
- **Model spend.** The current global spend cap becomes per-tenant (and per-user) budgets with alerts, in
  addition to the per-investigation limits that exist now.

### Audit logging

Every tool call writes an audit record: time, tenant, user, OAuth client id, token identifier (`jti` or
equivalent, never the token), tool, validated arguments, backend queries issued and their row counts,
result size, outcome and latency. Results themselves are not stored in the audit log (a hash and size are
enough), because they contain customer telemetry. The log is append-only, separate from debug logs,
tamper-evident, retained per policy, and queryable per tenant. The step logs in this repo already record
every call and result for the agent; what is missing is identity, tenancy and immutability.

### Prompt-injection defenses

The assumption throughout is that **telemetry is attacker-controlled text** (a user agent string, an error
message, a span name) that is routed into the model's context by design.

What exists now:

- Telemetry is returned inside a labelled `<telemetry_data>` block with a data-only warning; values are
  cleaned (control and bidi characters stripped, closing tags defanged, long values cut).
- The system prompt states that telemetry is data and instructions in it must never be followed, and that
  instruction-like text should be reported as an observation.
- **Read-only tools.** There is no tool that writes, sends, or fetches a URL, so a successful injection's
  worst case is a wrong answer, not an action. This is the most important property and it must be
  protected: no write tool should be added without the controls below.
- Inputs are schema-validated; reports are structured and validated.
- The eval suite has an injection scenario (a payload in `user_agent.original` demanding a false root
  cause and a canary string), scored deterministically.
- The memory sanitizer, provenance and verify-before-trust prompting limit a stored injection's reach.
- The anti-fabrication check measures whether claims trace to real tool output.

What more is needed in production:

- **Content isolation.** Keep free-text fields out of the main agent's context unless needed; let a
  separate, tool-less extraction step read untrusted strings and return only structured facts (the
  dual-model pattern), or restrict free-text fields to an allow-list. The BubbleUp output is where
  attacker-chosen strings are most likely to surface.
- **Allow-listed tool sequences and arguments.** A policy layer between model and tools that rejects
  calls outside the investigation toolset, validates arguments against the tenant's schema, and bounds
  call volume, so injected text cannot steer the agent into expensive or unrelated actions.
- **Output checks.** Promote the eval's grounding check to a runtime gate: services in the report must
  exist in the dataset, cited values must appear in tool results, and a report is held or flagged when
  they do not. Strip or block URLs, markdown images and other egress channels from output, and scan for
  canary strings and for text that echoes known payload shapes.
- **Human confirmation for any future write tool** (opening a ticket, paging, changing a flag, rolling
  back): the model proposes, a human approves, and the approval is bound to the exact arguments shown.
- **Least privilege and separation.** Per-tenant credentials, no ambient cloud credentials in the agent
  process, and no path from tool output to the memory store except through the sanitizer and review.
- **Continuous red-teaming.** Many more injection scenarios (other fields, other languages, multi-step
  and indirect payloads, instructions phrased as facts) in the eval suite and a regression gate on them.
  Today there is exactly one, and a lexical sanitizer is not a defense against a determined attacker.

### Observability of the system itself

The server and the agent should be traced with OpenTelemetry, ideally into the same kind of backend they
query. The relevant conventions are the OpenTelemetry **GenAI semantic conventions** and their MCP
section (now maintained in `open-telemetry/semantic-conventions-genai`; their status is *Development*,
so pin a version and expect renames):

- An `invoke_agent` span per investigation, with the run id and scenario or alert id.
- A `chat {model}` span per model call, with `gen_ai.operation.name=chat`, `gen_ai.request.model`, and
  token usage (`gen_ai.usage.input_tokens`, which includes cached tokens, and output tokens), plus cache
  read and write counts and cost as custom attributes.
- An `execute_tool {tool}` span per harness tool call (`gen_ai.tool.name`, `gen_ai.tool.call.id`), and on
  the server a span per MCP request named `{mcp.method.name} {target}` (for example `tools/call
  run_query`) with `mcp.method.name`, the session id where applicable, and an error status carrying the
  JSON-RPC error. Trace context travels in the MCP request's `params._meta`, so client and server spans
  join up.
- Metrics for tokens, cost, steps per investigation, tool latency and error rate, backend query counts and
  rate-limit rejections. Prompt and tool-result content is opt-in in those conventions and should stay off
  by default: it is customer data.

The existing step log maps almost one to one onto these spans (`model_call` to `chat`, `tool_result` to
`execute_tool`), so this is mostly a change of sink.

### Secrets handling

Today: keys come from environment variables, `.env` is gitignored, `.env.example` holds only names, the
spend ledger and step logs contain no credentials, and the collector's Honeycomb key is passed through the
host environment and never written to a file. For production: a secrets manager (not env files), separate
credentials per environment and per tenant, minimum permissions, short-lived or rotated keys, a
provider-side spending limit on the model key as a second cap behind the application's, no secrets in
logs, traces or tool results, and secret and PII scanning of telemetry on its way into the model's context
(telemetry can contain tokens and personal data; the demo's collector has redaction processors, a real
deployment should have its own).

### Evaluation in CI

- **On every PR, no key needed:** the unit and pipeline tests (`uv run pytest`; the suite drives the whole
  investigate-and-score pipeline with a fake model) plus a lint pass.
- **On PRs that touch prompts, tools or the harness:** a small live eval (a fixed subset of scenarios, a
  few runs each, against a pinned model id) with a hard `SPEND_CAP_USD` for the CI job, compared to the
  stored baseline with `--compare-to`. Gates: no new false alarm on the negative control, zero obeyed
  injections, fabrication rate not up, and accuracy not down by more than the measured run-to-run noise
  (report `k/n` and require a drop larger than the variance observed on identical reruns, not a single
  point).
- **Nightly or on release:** the full suite at 3 or more runs per scenario, appended to the results log,
  with cost, steps and tokens per investigation tracked as regressions in their own right.
- **Always:** archive run logs as build artifacts; re-score archived runs without a model when the scoring
  rules change (`rescore`); verify fixture integrity (hashes) so the data under test is the data on
  record; and re-run the whole suite when the model id changes, because a model change is a change to
  the system.
