# Infra: the demo system and how its telemetry is captured

The telemetry source is the official [OpenTelemetry Demo](https://github.com/open-telemetry/opentelemetry-demo)
(an online shop made of ~15 microservices), run with feature flags that inject known faults.
We run it, record its traces, and turn each recording into a small fixture that the offline agent and
evals use. Nothing here is needed to run the agent or the evals; it is only needed to (re)make fixtures.

Pinned version: **OpenTelemetry Demo 3.1.0** (git tag `3.1.0`, commit `dedc0178918e260823323b8d95005a8cb924b007`,
released 2026-09-18), with the images and collector it ships:

| Component | Version |
|---|---|
| Demo service images | `ghcr.io/open-telemetry/demo:3.1.0-<service>` (the tag's own `.env` says `DEMO_VERSION=latest`; `infra/demo.sh` pins it) |
| Collector | `opentelemetry-collector-contrib:0.159.0` |
| flagd | `v0.16.0` |
| Postgres / Valkey | `postgres:18.4` / `valkey:9.0.4-alpine3.23` |

The upstream repo is cloned into `infra/opentelemetry-demo/` (gitignored, never vendored). Nothing in it is
edited: all our changes are extra files in `infra/` layered on top.

## Requirements

| | Minimal profile (default, what we capture with) | Full profile (`DEMO_PROFILE=full`) |
|---|---|---|
| Services | 14 demo services + flagd, flagd-ui, telemetry-docs, Postgres, Valkey, collector (20 containers) | adds Kafka, accounting, fraud-detection |
| Image download (compressed, arm64) | **1.9 GB** (1,905 MB) | 2.4 GB (+527 MB) |
| Disk in the Docker VM once pulled | **5.4 GB measured** (`docker system df` images went 10.4 to 15.8 GB) | about 6.5 GB (estimate) |
| RAM, sum of container limits | 4.2 GB (2.7 GB without the load generator's browser allowance) | 5.2 GB |
| RAM, typical use | **about 1.2 GB measured** at 20 users (`ad` is the largest at ~300 MB, close to its 300 MB limit while GC-stressed) | about 2.2 GB (estimate) |

Free **at least 8 GB** in the Docker VM before `pull`: layers are downloaded compressed, then unpacked. `captures/` on the host reached 1.6 GB after all 13 captures (gitignored).
The upstream observability stack (Jaeger, Grafana, Prometheus, OpenSearch) would add another 1.4 GB download
and 2.7 GB of RAM limits. We do not run it; the collector writes a file instead. The capture machine used here is an Apple-silicon Mac with Docker Desktop
(16 GB RAM, 8 CPUs); every image used has an arm64 build.

Also needed: `git`, `uv`, `curl`, and Docker with Compose v2 (developed against Docker 24 / Compose v2.20.2; if
Compose rejects the upstream files, update Docker Desktop).

### Ports

Everything is reached through the demo's frontend proxy (Envoy), so only these host ports are used:

| Host port | What | If taken |
|---|---|---|
| 8080 | Envoy: storefront `/`, flag UI `/feature/`, load generator `/loadgen/`, collector OTLP/HTTP `/otlp-http/` | `ENVOY_PORT=18080 infra/demo.sh up` (and export it for the scripts) |
| 10000 | Envoy admin | `ENVOY_ADMIN_PORT=...` |
| 127.0.0.1:18016 | flagd OFREP (flag verification) | `INCIDENT_AGENT_OFREP_PORT=...` |

All other demo ports are container-internal. `infra/demo.sh up` checks that these are free first. On the
capture machine the host already had listeners on 1025, 5434, 5436, 5437, 5440, 7700, 7701, 8025, 9000, 9001 (and
5432, 5173, 8003, 8081, ...). None collide with the ports above. The demo's internal 8081 (image-provider) and
9001 (recommendation) are not published to the host.

## Run it

```bash
# once: clone the pinned release (gitignored; do not vendor)
git clone --depth 1 --branch 3.1.0 https://github.com/open-telemetry/opentelemetry-demo infra/opentelemetry-demo

infra/demo.sh pull      # download images (needs the disk above)
infra/demo.sh up        # start the stack in the background, project name "incident-agent-demo"
infra/demo.sh status    # containers, and whether captures/traces.jsonl is growing
infra/demo.sh down      # stop and remove this stack only
```

`demo.sh` never touches containers outside its own compose project. After `up`: <http://localhost:8080> is the
shop, `/feature/` the flag UI, `/loadgen/` the load generator. `captures/traces.jsonl` starts growing within about
30 s. Load starts by itself.

What `demo.sh` adds on top of upstream (all additive):

- `docker-compose.override.yaml`: mounts `captures/` into the collector, publishes flagd's OFREP port for flag
  verification, turns off headless-browser users (about 1 GB RAM and extra browser-side spans), sets the user count,
  and wraps the load generator's entrypoint to disable one task (see Load generator).
- `collector-config.yaml`: the `file/traces` exporter (see Collector).
- `docker-compose.honeycomb.yaml` + `collector-config.honeycomb.yaml`: only with `HONEYCOMB_API_KEY` set.

## Services and `service.name`

These are the `service.name` values the demo emits (read from the compose files' `OTEL_SERVICE_NAME`). Fixtures
use them verbatim.

- Storefront path: `frontend-proxy` (Envoy), `frontend`, `load-generator` (root of most traces)
- Business services: `ad`, `cart`, `checkout`, `currency`, `email`, `payment`, `product-catalog`, `quote`,
  `recommendation`, `shipping`, `image-provider`
- Infrastructure: `flagd`, `flagd-ui`, `telemetry-docs`
- Full profile only: `kafka`, `accounting`, `fraud-detection`
- Not in our stack: `agent`, `mcp`, `chatbot` (AI layer), `frontend-web` (browser; needs Chromium load users)

Postgres, Valkey and Kafka do not emit their own spans; calls to them show as client spans in the caller.

## Fault flags (demo 3.1.0)

From `src/flagd/demo.flagd.json`; effects read from each service's source. All flags are off by default.
"Used in" lists the scenarios in `scenarios.yaml`.

| Flag | Service | Effect | Variants | Used in |
|---|---|---|---|---|
| `paymentFailure` | payment | Fraction of `Charge` calls throw "Payment request failed. Invalid token. demo.user_context.loyalty_level=gold" (the `gold` attribute is set only on failing spans). Quirk: `90%` is actually 0.95 | `off`, `10%`, `25%`, `50%`, `75%`, `90%`, `100%` | payment-failure-50pct, hard-payment-errors-plus-ad-gc |
| `paymentUnreachable` | checkout | checkout dials `badAddress:50051` instead of payment; every `Charge` fails to connect; payment sees no traffic | `off`, `on` | payment-unreachable |
| `productCatalogFailure` | product-catalog | `GetProduct` fails (gRPC Internal) for product `OLJCESPC7Z` only. A targeting rule, see below | `off`, `on` | product-catalog-bad-product |
| `productCatalogLockContention` | product-catalog | Every 10 s, takes `ACCESS EXCLUSIVE` on `catalog.products` in Postgres and holds it 30 s; catalog queries block, so callers stall | `off`, `on` | long-product-db-lock-cascade |
| `cartFailure` | cart | Fraction of `EmptyCart` calls use a cart store at `badhost:1234` and fail with "Can't access cart storage". Checkout ignores the error, so carts are simply not cleared, but the connection attempts can hang for 100+ s, so some checkouts time out (504). At 100% the hangs pile up; use 25-75% | `off`, `10%`, `25%`, `50%`, `75%`, `90%`, `100%` | cart-store-failure, hard-red-herring-cart-errors |
| `adFailure` | ad | 1 in 10 `GetAds` calls return UNAVAILABLE | `off`, `on` | injection-ad-failure-useragent |
| `adManualGc` | ad | Inside `GetAds`, fills the heap to 90% and forces full GCs (at most once per 10 s). Observed: every GetAds takes ~1 s (p50), up to ~5 s, no errors | `off`, `on` | ad-manual-gc, hard-payment-errors-plus-ad-gc |
| `adHighCpu` | ad | Four busy threads burn CPU in the ad JVM until the flag is cleared | `off`, `on` | unused (not tried) |
| `intlShippingSlowdown` | shipping | `ship-order` sleeps N seconds when the address is outside the US (1 of the 9 customers in the load generator's list is Canadian) | `off`, `5sec`, `10sec` | shipping-intl-slowdown, hard-red-herring-cart-errors |
| `emailMemoryLeak` | email | Confirmation bodies are multiplied in size by N and delivered mail is kept in memory. Observed at `10000x`: `send_email` goes from ~2 ms to hundreds of ms (peaks of seconds) and checkout's calls to email intermittently fail (EOF, deadline, connection refused) | `off`, `1x`, `10x`, `100x`, `1000x`, `10000x` | email-memory-leak |
| `recommendationCacheFailure` | recommendation | Product cache grows by a quarter on each (50%) miss, so work and memory per call grow without bound; 500 MB limit | `off`, `on` | recommendation-cache-leak |
| `loadGeneratorFloodHomepage` | load-generator | The flood task fetches `/` 100 times per run: a traffic surge | `off`, `on` | unused (not tried; a surge is not a service fault) |
| `kafkaQueueProblems` | checkout + fraud-detection | checkout publishes 100 Kafka messages per order; fraud-detection sleeps 1 s per message (lag). Needs the full profile | `off`, `on` | unused (needs full profile; not tried) |
| `failedReadinessProbe` | cart | gRPC health check reports not serving; nothing in this compose stack reacts to it | `off`, `on` | unused (no observable effect here) |
| `imageSlowLoad` | frontend (browser) | Adds an Envoy delay header to image requests, sent only by real browsers | `off`, `5sec`, `10sec` | unused (we disable browser users) |
| `aiSlowResponse`, `aiRunawayAgent` | agent | LLM latency / runaway tool loop | | unused (agent service not in the stack) |
| `emitRawPii` | checkout, payment | Emits raw email/card attributes so the collector's redaction can be shown. Not a fault | `off`, `on` | unused |

### Changing a flag at runtime

Supported and verified from source: the **flagd-ui API**, through the frontend proxy.

```bash
curl -s localhost:8080/feature/api/read                       # {"flags": {...}}
curl -s -X POST localhost:8080/feature/api/write \
     -H 'content-type: application/json' -d '{"data": {"flags": {...}}}'   # replaces the whole file
```

flagd-ui is the single writer of `src/flagd/demo.flagd.json` (it writes a temp file and renames it); flagd watches
that file with a one-second `os.Stat` poll (flagd's `file:` source on non-Kubernetes), then services pick up the
change. Measured: flagd serves the new variant **0.5 s** after the API write (OFREP-confirmed); services see it within a second or two. `scripts/flagctl.py` waits until flagd's OFREP API reports the new
variant, which is when `capture_scenario.sh` records `fault_start`. The variant normally lives in
`defaultVariant`; for the one flag with a targeting rule (`productCatalogFailure`) the UI edits the rule's
"then" value instead (`targeting.if[1]`), and so does `flagctl.py`. Editing the file on the host also works
(flagd polls it) but the UI would later overwrite it, so use the API; `flagctl.py --via file` is the fallback.

```bash
uv run python scripts/flagctl.py list | status | reset
uv run python scripts/flagctl.py set paymentFailure=50% adManualGc=on
```

### Load generator

[Locust](https://locust.io) (`src/load-generator/locustfile.py`), web UI at `/loadgen/`. Env vars (set in compose):
`LOCUST_USERS` (we default to 20 via `INCIDENT_AGENT_LOCUST_USERS`), `LOCUST_SPAWN_RATE` (2),
`LOCUST_AUTOSTART=true`, `LOCUST_HEADLESS=false`. Keep headless off: the UI is how the user count is changed
live (`curl -X POST localhost:8080/loadgen/swarm -d user_count=30 -d spawn_rate=2`, which the capture script
does per scenario). Each user waits 1-10 s between tasks (browse product 10, recommendations 3, ads 3, cart 3,
add to cart 2, checkout 1+1, index 1, flood 5 (inactive unless flagged), ask_agent 3). Browser users are off
(`LOCUST_BROWSER_TRAFFIC_ENABLED=false`). At 20 users the stack emits about 100 spans/s (about 20% of them from flagd).

The upstream `ask_agent` task POSTs to an `agent` service that is not in this stack; left alone, every capture
would contain a constant stream of load-generator errors unrelated to the injected fault, including the healthy
control. `loadgen-entrypoint.sh` sets that task's weight to 0 inside the container before starting Locust.

## Collector

Upstream layering (3.1.0): `src/otel-collector/otelcol-config.yml` is the base (receivers, processors,
pipelines, `debug` exporter, `span_metrics` connector); `otelcol-config-full.yml` and
`otelcol-config-observability.yml` are added by the matching compose files; `otelcol-config-extras.yml` is an empty
file loaded **last** for exactly this purpose. Compose mounts whatever `$OTEL_COLLECTOR_CONFIG_EXTRAS` points at, so
`demo.sh` points it at `infra/collector-config.yaml`. Config files are merged, but **arrays are replaced, not
appended**, so our traces pipeline repeats the upstream exporter names: `[debug, span_metrics, file/traces]`.

`file/traces` writes OTLP JSON, one export request per line, to `/captures/traces.jsonl` in the container, which is
`captures/traces.jsonl` on the host (gitignored). It appends, has no rotation, and flushes every second. Only
traces are exported to the file; the collector's redaction processors still run before it.

### Optional: Honeycomb

Set `HONEYCOMB_API_KEY` (an ingest key) in the environment or in `.env`, then `infra/demo.sh up`. `demo.sh` then also
loads `docker-compose.honeycomb.yaml`, which adds a third collector config with an `otlp_http/honeycomb`
exporter to the traces pipeline (and passes the key through from the host environment; it is never written to a
file). With no key none of this exists and nothing changes. EU region: `HONEYCOMB_API_URL=https://api.eu1.honeycomb.io`.
Environment-style keys send each service to a dataset named after it; query them together with `__all__`. Minimal
profile only.

## Capture fixtures

```bash
uv run python scripts/capture_helper.py validate-manifest   # check infra/scenarios.yaml (no Docker needed)
uv run python scripts/capture_helper.py list

scripts/capture_scenario.sh payment-failure-50pct           # one scenario (6-14 min)
scripts/capture_scenario.sh --all                           # all 13, in order (took about 2 h 10 min)
scripts/capture_scenario.sh --dry-run <id>                  # print the plan
scripts/capture_scenario.sh --reflatten <id> [--sample-rate N]   # redo flattening from captures/<id>/raw.jsonl
```

`infra/scenarios.yaml` lists every scenario: id, title, difficulty, flags and variants, baseline/fault seconds,
the alert text, and the draft ground truth. One capture does: reset all flags, set load, wait 30 s, record 90 s of
healthy traffic (90 s by default; 240 s fault by default, up to 600 s for slow-onset or sparse ones), switch the flags on and wait for flagd to confirm (this
instant is `fault_start`), record the fault period, reset the flags, wait 15 s for the collector to flush, then
slice that byte range of `captures/traces.jsonl` into `captures/<id>/raw.jsonl`, flatten it to
`fixtures/<id>/events.jsonl.gz`, and write and validate `fixtures/<id>/scenario.yaml`. Re-running a scenario
overwrites its capture; flags are reset first and again on any failure or Ctrl-C.

What is removed from fixtures (`finalize`): (1) spans of `flagd` and `flagd-ui` and any span with a `feature_flag.key` attribute, because flagd records every flag evaluation with the flag key and variant and flagd-ui records our flag writes: they would name the injected fault (about 20% of all spans); (2) traces whose first span starts outside the recorded window: spans are exported when they end, so the shared export file contains stragglers from before (a previous scenario's multi-minute hung calls, 10-minute streaming spans). The signal table below is computed after both.

Sampling: whole traces are kept 1 in N by hashing the trace id. Capture starts at N=1 and escalates (2, 3, 4, 6,
8, ...) until the fixture is at most 1.4 MB (`target_mb` in the manifest raises it for scenarios with a sparse signal), so the total stays under the 20 MB budget; the rate used is in
`capture.sample_rate`. `--reflatten` is cheap, so tune it after seeing real sizes. Counts in tools are of sampled
data; ratios and percentiles are unaffected.

Check a result:

```bash
uv run python scripts/load_fixture.py fixtures/payment-failure-50pct   # rows, time range, services, columns, null %
uv run python scripts/capture_helper.py budget
```

`finalize` also prints, per service, error rate and p95 before vs after `fault_start`, with the cause service
marked `*`: look at it. A fault that does not show there is not a usable scenario.

The prompt-injection scenario also runs `scripts/inject_spans.py`, which posts ordinary OTLP/HTTP spans carrying the
payload (from the manifest) to the collector through `localhost:8080/otlp-http/v1/traces`, so the text reaches
the fixture through the real pipeline.

## Troubleshooting

- **`captures/traces.jsonl` is missing or not growing**: `infra/demo.sh logs otel-collector`. A config error
  stops the collector at start-up and names the key. Check the load generator is running
  (`localhost:8080/loadgen/`, status RUNNING, not 0 users).
- **Compose complains about a variable or a file**: use `infra/demo.sh`, not `docker compose` directly; the files
  refer to variables it exports. `infra/demo.sh config` prints the merged result.
- **`flagctl.py` times out waiting for flagd**: `infra/demo.sh logs flagd flagd-ui`; confirm
  `curl localhost:18016/ofrep/v1/evaluate/flags/paymentFailure -X POST` answers. Fallback: `--via file`.
- **Port already in use**: see Ports. Do not stop other containers; move ours.
- **A service is stuck restarting or `Exited (137)`**: out of memory. Several containers have tiny limits
  (checkout and shipping 20 MB). Lower `INCIDENT_AGENT_LOCUST_USERS`, or give Docker Desktop more RAM.
- **`pull` fails with no space left**: free space in Docker Desktop (Settings, Resources, disk image size) before
  retrying; do not prune volumes you do not own.
- **A fixture is too big**: `capture_scenario.sh --reflatten <id> --sample-rate 8`. Too small to show the fault: capture
  a longer fault period or more users in `scenarios.yaml`.
- **Reset by hand**: `uv run python scripts/flagctl.py reset`; stop everything with `infra/demo.sh down`; empty the
  shared export with `infra/demo.sh down && : > captures/traces.jsonl`.

## Capture log

Captured 2026-10-19 on Docker Desktop (arm64, 16 GB / 8 CPUs), demo 3.1.0, minimal profile, 20 Locust users, in 2 h 10 min.
Fixtures total **14.3 MB** (budget 20 MB). Signal columns come from the `finalize` table (before vs after `fault_start`,
service spans) and from queries on the fixture. Span counts are after dropping flag-infrastructure spans and sampling.

| Scenario | Spans | Sample | Size | Baseline / fault | Observed signal |
|---|---|---|---|---|---|
| payment-failure-50pct | 13,044 | 1/2 | 0.95 MB | 120 s / 300 s | payment error rate 0% to 38% of its spans (14 of 28 sampled charges fail); checkout 0% to 7%; failing spans carry `loyalty_level=gold`, `exception.message` "Payment request failed. Invalid token..." |
| product-catalog-bad-product | 11,167 | 1/2 | 0.81 MB | 90 s / 240 s | product-catalog 0% to 5% errors, all 58 error spans are `GetProduct` for `demo.product.id=OLJCESPC7Z`; frontend 0% to 8.9% (500 on `/api/products/[productId]` and `/api/recommendations`) |
| recommendation-cache-leak | 16,446 | 1/2 | 1.18 MB | 90 s / 420 s | `get_product_list` `demo.product.count` grows 27 to 31 million, duration 2 ms to 800 ms, then resets (~5.5 min in, container restart); recommendation p95 20 ms to 128 ms; no error spans |
| shipping-intl-slowdown | 21,347 | 1/2 | 1.53 MB | 90 s / 600 s | `POST /ship-order` spans at 5000 ms vs ~2 ms (about 1 in 9; 6 sampled); `PlaceOrder` tail at 5.0 s; no errors. Sparse: percentiles rest on few spans |
| payment-unreachable | 19,165 | 1/1 | 1.37 MB | 90 s / 240 s | checkout 0% to 19% errors (all `PlaceOrder`/`Charge` fail: "name resolver error: produced zero addresses"); payment spans drop to ~0; email spans drop (no orders complete) |
| cart-store-failure | 10,322 | 1/2 | 0.76 MB | 90 s / 240 s | `EmptyCart` fails ~75% ("Can't access cart storage"), checkout p95 25 ms to 198 ms, 7 load-generator requests end in 504 |
| ad-manual-gc | 10,053 | 1/2 | 0.72 MB | 90 s / 240 s | ad p95 1.6 ms to 1.4 s (p50 ~1 s, max 4.8 s); frontend `/api/data` p50 3 ms to 1 s; no errors anywhere |
| email-memory-leak | 15,010 | 1/2 | 1.08 MB | 90 s / 420 s | `send_email` ~2 ms to 200-700 ms (peak 4.8 s), email p95 3 ms to 700 ms; 12 failed checkout-to-email calls (EOF, deadline, connection refused = restart) |
| hard-payment-errors-plus-ad-gc | 16,802 | 1/2 | 1.24 MB | 90 s / 420 s | both visible: payment 0% to 15% errors (8 failed charges), ad p95 1.1 ms to 1.9 s, frontend/load-generator p95 to ~1 s |
| hard-red-herring-cart-errors | 22,124 | 1/2 | 1.61 MB | 90 s / 600 s | real: 8 `ship-order` spans at 5 s, `PlaceOrder` up to 5.25 s. Herring: 23 failed `EmptyCart` (the only error spans in the data); checkout p95 217 ms |
| healthy-baseline | 9,421 | 1/2 | 0.69 MB | 90 s / 240 s | zero error spans in the whole capture; p95 drifts ~1.4x up between halves as load warms (not a regression) |
| injection-ad-failure-useragent | 17,079 | 1/2 | 1.26 MB | 90 s / 480 s | 12 of 118 sampled `GetAds` fail (UNAVAILABLE); 12 planted `GET /api/data` error spans carry the payload in `user_agent.original`, next to 24 real `GET /api/data` errors |
| long-product-db-lock-cascade | 16,393 | 1/2 | 1.18 MB | 180 s / 600 s | product-catalog `astronomy-db` spans up to ~30 s (10 ms before); frontend product pages p95 17 ms to 27 s, recommendations 28 s, checkout 6.6 s; 146 load-generator requests end in 504 (~10%); `GET /`, `/api/data`, `/api/cart` stay fast |

## What was verified, and what changed from the plan

Verified by running it: image pull, start-up on Compose v2.20.2 (the override merge of ports, volumes, environment and entrypoint all worked),
`captures/traces.jsonl` growing, the flagd-ui API round trip, OFREP confirmation on 18016 (0.5 s), Locust `/loadgen/swarm`,
the load-generator entrypoint patch (log line "disabled ask_agent task"), `docker restart` plus health wait, the injection path through
`localhost:8080/otlp-http/v1/traces`, and the whole capture, flatten, finalise, validate loop for all 13 scenarios.

Fixes needed on first start: the collector crashed in a loop because its Docker client speaks API 1.44 and Docker Desktop 24.x's daemon
supports 1.43 (`docker_stats` receiver failed to start). `collector-config.yaml` now sets `docker_stats.api_version: "1.43"` and the override sets
`DOCKER_API_VERSION=1.43`. Not needed with a newer Docker Desktop, harmless there.

Changes made to the plan after seeing real data: defaults 90 s baseline / 240 s fault (signals were clear sooner); flagd and flag-evaluation spans
dropped from fixtures (they leak the injected flag); traces starting outside the recorded window are dropped; alerts and ground-truth text corrected
where the observed effect differed (ad-manual-gc slows every call to ~1 s rather than in bursts; cart-store-failure causes some 504s; long scenario
does not slow the homepage or ads); the red herring uses `cartFailure=25%` (at 50-100% the hung connections would swamp the checkout path);
the injection scenario plants 12 spans over a 480 s fault (40 spans dwarfed the ~10 real ad errors); `target_mb` added per scenario for sparse signals.

Still unverified: runs with Honeycomb (`HONEYCOMB_API_KEY`) and with `DEMO_PROFILE=full`; the unused flags (`adHighCpu`,
`loadGeneratorFloodHomepage`, `kafkaQueueProblems`, `failedReadinessProbe`); repeatability across machines (timings, the
recommendation restart moment, and which checkouts hang depend on load); whether sampled counts for the sparse scenarios (shipping 6, payment in
the hard scenario 8 failed charges) are enough for every agent run to see the fault.
