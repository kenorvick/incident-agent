#!/usr/bin/env bash
# Capture one scenario from the running OpenTelemetry Demo into a fixture.
#
#   scripts/capture_scenario.sh <scenario-id> [options]
#   scripts/capture_scenario.sh --all [options]     every scenario in infra/scenarios.yaml, in order
#
# Steps for one scenario (all parameters come from infra/scenarios.yaml):
#   1. check the stack is up and the collector is writing captures/traces.jsonl
#   2. reset every feature flag to off, set the load level, restart stateful containers if the
#      scenario asks, then let things settle
#   3. remember the byte offset of captures/traces.jsonl, record BASELINE seconds of healthy load
#   4. switch the scenario's flags on, wait until flagd confirms, note `fault_start` (UTC)
#      (injection scenarios also plant their spans now), record FAULT seconds
#   5. reset all flags, wait for the collector to flush, copy the bytes recorded to
#      captures/<id>/raw.jsonl (whole lines only)
#   6. flatten -> fixtures/<id>/events.jsonl.gz, write + validate fixtures/<id>/scenario.yaml
#
# Safe to re-run: it starts from "all flags off", writes outputs atomically, overwrites the
# previous capture of the same id, and a trap resets all flags if it is interrupted or fails.
# The shared export file is never truncated while the collector runs (a host-side truncate of a
# file the container appends to is not reliably coherent); the recorded offset isolates this run.
#
# Options
#   --reflatten        no Docker needed: redo step 6 from captures/<id>/raw.jsonl
#   --sample-rate N    force 1-in-N trace sampling instead of the automatic size-budget choice
#   --settle SECONDS   pause after reset before recording (default 30)
#   --flush SECONDS    wait after the fault for the collector to flush (default 15)
#   --dry-run          print the plan and exit
#
# Environment: ENVOY_PORT (8080), INCIDENT_AGENT_OFREP_PORT (18016), CAPTURES_DIR, FIXTURES_DIR.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

export CAPTURES_DIR="${CAPTURES_DIR:-$REPO_DIR/captures}"
export FIXTURES_DIR="${FIXTURES_DIR:-$REPO_DIR/fixtures}"
DEMO_URL="${DEMO_URL:-http://localhost:${ENVOY_PORT:-8080}}"
EXPORT_FILE="$CAPTURES_DIR/traces.jsonl"

SETTLE_SECONDS=30
FLUSH_SECONDS=15
REFLATTEN=0
DRY_RUN=0
RUN_ALL=0
SAMPLE_RATE_OVERRIDE=""
SCENARIO_ID=""

die() { echo "error: $*" >&2; exit 1; }
log() { echo "[$(date -u +%H:%M:%S)] $*"; }
py() { uv run --quiet python "$@"; }

usage() { sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --all) RUN_ALL=1 ;;
    --reflatten) REFLATTEN=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --sample-rate) SAMPLE_RATE_OVERRIDE="${2:?--sample-rate needs a value}"; shift ;;
    --settle) SETTLE_SECONDS="${2:?--settle needs a value}"; shift ;;
    --flush) FLUSH_SECONDS="${2:?--flush needs a value}"; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) die "unknown option $1 (try --help)" ;;
    *) [[ -z "$SCENARIO_ID" ]] || die "only one scenario id allowed"; SCENARIO_ID="$1" ;;
  esac
  shift
done

command -v uv >/dev/null || die "uv not found (https://docs.astral.sh/uv/)"

if [[ "$RUN_ALL" == 1 ]]; then
  [[ -z "$SCENARIO_ID" ]] || die "--all and a scenario id are mutually exclusive"
  passthrough=()
  [[ "$REFLATTEN" == 1 ]] && passthrough+=(--reflatten)
  [[ "$DRY_RUN" == 1 ]] && passthrough+=(--dry-run)
  [[ -n "$SAMPLE_RATE_OVERRIDE" ]] && passthrough+=(--sample-rate "$SAMPLE_RATE_OVERRIDE")
  passthrough+=(--settle "$SETTLE_SECONDS" --flush "$FLUSH_SECONDS")
  failed=()
  while IFS= read -r id; do
    echo "=================== $id ==================="
    "${BASH_SOURCE[0]}" "$id" "${passthrough[@]}" || failed+=("$id")
  done < <(py scripts/capture_helper.py ids)
  py scripts/capture_helper.py budget || true
  if [[ ${#failed[@]} -gt 0 ]]; then die "failed: ${failed[*]}"; fi
  exit 0
fi

[[ -n "$SCENARIO_ID" ]] || { usage; exit 2; }

# ---- load the plan (values are shell-quoted by the helper) -----------------------------------
PLAN="$(py scripts/capture_helper.py show "$SCENARIO_ID" --shell)" || exit 1
eval "$PLAN"   # sets BASELINE_SECONDS FAULT_SECONDS USERS SAMPLE_RATE FLAG_ARGS RESTART_BEFORE INJECT DIFFICULTY

if [[ "$DRY_RUN" == 1 ]]; then
  echo "scenario:        $SCENARIO_ID ($DIFFICULTY)"
  echo "flags:           ${FLAG_ARGS:-(none)}"
  echo "users:           $USERS"
  echo "baseline/fault:  ${BASELINE_SECONDS}s / ${FAULT_SECONDS}s (+${SETTLE_SECONDS}s settle, ${FLUSH_SECONDS}s flush)"
  echo "restart before:  ${RESTART_BEFORE:-(none)}"
  echo "inject spans:    ${INJECT:-no}"
  echo "start sample 1/: ${SAMPLE_RATE_OVERRIDE:-$SAMPLE_RATE}"
  echo "outputs:         $CAPTURES_DIR/$SCENARIO_ID/raw.jsonl, $FIXTURES_DIR/$SCENARIO_ID/"
  exit 0
fi

FINALIZE_ARGS=("$SCENARIO_ID")
[[ -n "$SAMPLE_RATE_OVERRIDE" ]] && FINALIZE_ARGS+=(--sample-rate "$SAMPLE_RATE_OVERRIDE")

if [[ "$REFLATTEN" == 1 ]]; then
  log "re-flattening $SCENARIO_ID from $CAPTURES_DIR/$SCENARIO_ID/raw.jsonl"
  py scripts/capture_helper.py finalize "${FINALIZE_ARGS[@]}"
  exit 0
fi

# ---- helpers for the live capture ------------------------------------------------------------
INJECT_PID=""
FLAGS_MAY_BE_ON=0

cleanup() {
  local status=$?
  if [[ -n "$INJECT_PID" ]] && kill -0 "$INJECT_PID" 2>/dev/null; then kill "$INJECT_PID" 2>/dev/null || true; fi
  if [[ "$FLAGS_MAY_BE_ON" == 1 ]]; then
    echo "cleanup: resetting all feature flags" >&2
    py scripts/flagctl.py --no-wait reset >&2 || echo "WARNING: could not reset flags; run: uv run python scripts/flagctl.py reset" >&2
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'echo "interrupted" >&2; exit 130' INT TERM

file_size() { wc -c <"$1" | tr -d ' '; }

wait_for() {  # wait_for SECONDS LABEL
  local remaining="$1" label="$2"
  log "$label: ${remaining}s"
  while (( remaining > 0 )); do
    local step=$(( remaining < 30 ? remaining : 30 ))
    sleep "$step"
    remaining=$(( remaining - step ))
    (( remaining > 0 )) && log "$label: ${remaining}s left"
  done
  return 0
}

wait_healthy() {  # wait_healthy CONTAINER  (up to 120s; containers without a healthcheck: 15s)
  local name="$1" status="" waited=0
  while (( waited < 120 )); do
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$name" 2>/dev/null || echo missing)"
    case "$status" in
      healthy) return 0 ;;
      none) sleep 15; return 0 ;;
      missing) die "container $name not found" ;;
    esac
    sleep 3; waited=$(( waited + 3 ))
  done
  die "container $name not healthy after 120s"
}

# ---- preflight -------------------------------------------------------------------------------
curl -fsS --max-time 5 "$DEMO_URL/feature/api/read" >/dev/null \
  || die "cannot reach $DEMO_URL/feature/api/read. Is the demo running? Start it with: infra/demo.sh up"
[[ -f "$EXPORT_FILE" ]] || die "$EXPORT_FILE does not exist. The collector's file exporter is not running (see infra/README.md, Troubleshooting)."
size_before="$(file_size "$EXPORT_FILE")"
sleep 8
size_after="$(file_size "$EXPORT_FILE")"
(( size_after > size_before )) \
  || die "$EXPORT_FILE is not growing: no telemetry is arriving. Is the load generator running? ($DEMO_URL/loadgen/)"

# ---- 1. all flags off, load level, restarts, settle ------------------------------------------
FLAGS_MAY_BE_ON=1
log "resetting all feature flags to off"
py scripts/flagctl.py reset

log "setting load to $USERS users"
curl -fsS --max-time 10 -X POST "$DEMO_URL/loadgen/swarm" -d "user_count=$USERS" -d "spawn_rate=2" >/dev/null \
  || echo "warning: could not set the Locust user count via $DEMO_URL/loadgen/swarm; continuing with whatever load is running" >&2

for svc in $RESTART_BEFORE; do
  log "restarting container $svc to clear in-process state"
  docker restart "$svc" >/dev/null
  wait_healthy "$svc"
done

wait_for "$SETTLE_SECONDS" "settling"

# ---- 2. baseline -----------------------------------------------------------------------------
mkdir -p "$CAPTURES_DIR/$SCENARIO_ID"
START_OFFSET="$(file_size "$EXPORT_FILE")"
RECORDING_START="$(py scripts/capture_helper.py now)"
log "recording from byte offset $START_OFFSET (baseline ${BASELINE_SECONDS}s)"
wait_for "$BASELINE_SECONDS" "baseline"

# ---- 3. fault --------------------------------------------------------------------------------
FAULT_START=""
if [[ -n "$FLAG_ARGS" ]]; then
  read -r -a flag_list <<<"$FLAG_ARGS"
  log "enabling: $FLAG_ARGS"
  py scripts/flagctl.py set "${flag_list[@]}"
  FAULT_START="$(py scripts/capture_helper.py now)"
  log "fault_start = $FAULT_START"
else
  log "no flags for this scenario (negative control): recording normal traffic"
fi

if [[ -n "$INJECT" ]]; then
  inject_for=$(( FAULT_SECONDS > 40 ? FAULT_SECONDS - 20 : FAULT_SECONDS ))
  log "planting injection spans over ${inject_for}s"
  py scripts/inject_spans.py --scenario "$SCENARIO_ID" --duration "$inject_for" &
  INJECT_PID=$!
fi

wait_for "$FAULT_SECONDS" "fault"

if [[ -n "$INJECT_PID" ]]; then
  wait "$INJECT_PID" || die "inject_spans.py failed; the injection scenario would have no payload"
  INJECT_PID=""
fi

# ---- 4. stop, flush, slice -------------------------------------------------------------------
log "resetting all feature flags to off"
py scripts/flagctl.py reset
FLAGS_MAY_BE_ON=0
RECORDING_END="$(py scripts/capture_helper.py now)"
wait_for "$FLUSH_SECONDS" "letting the collector flush"
END_OFFSET="$(file_size "$EXPORT_FILE")"

py scripts/capture_helper.py slice "$EXPORT_FILE" "$START_OFFSET" "$CAPTURES_DIR/$SCENARIO_ID/raw.jsonl" --end "$END_OFFSET"
[[ -s "$CAPTURES_DIR/$SCENARIO_ID/raw.jsonl" ]] || die "captured no telemetry between offsets $START_OFFSET and $END_OFFSET"

py scripts/capture_helper.py save-meta "$SCENARIO_ID" \
  --baseline-seconds "$BASELINE_SECONDS" --fault-seconds "$FAULT_SECONDS" \
  --fault-start "${FAULT_START:-none}" \
  --recording-start "$RECORDING_START" --recording-end "$RECORDING_END" \
  --captured-at "$(py scripts/capture_helper.py now)"

# ---- 5. flatten, write scenario.yaml, validate -----------------------------------------------
py scripts/capture_helper.py finalize "${FINALIZE_ARGS[@]}"
log "done: fixtures/$SCENARIO_ID"
