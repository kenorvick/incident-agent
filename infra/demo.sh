#!/usr/bin/env bash
# Start/stop the pinned OpenTelemetry Demo with incident-agent's collector and load settings.
#
#   infra/demo.sh pull        download images (do this first; needs free Docker disk, see README)
#   infra/demo.sh up          start the stack in the background
#   infra/demo.sh status      show containers and whether telemetry is reaching captures/
#   infra/demo.sh logs [svc]  follow logs
#   infra/demo.sh restart svc... restart individual containers (e.g. email recommendation)
#   infra/demo.sh down        stop and remove this stack (only this compose project)
#   infra/demo.sh config      print the resolved compose config (for debugging)
#
# Environment (all optional):
#   DEMO_PROFILE=minimal|full          default minimal (full adds Kafka, accounting, fraud-detection)
#   INCIDENT_AGENT_LOCUST_USERS=20     simulated users at start-up
#   ENVOY_PORT=8080                    host port of the frontend proxy (storefront, /feature, /loadgen)
#   HONEYCOMB_API_KEY=...              also export traces to Honeycomb (minimal profile only)
set -euo pipefail

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$INFRA_DIR")"
DEMO_DIR="$INFRA_DIR/opentelemetry-demo"
PINNED_TAG="3.1.0"
PROJECT="incident-agent-demo"
PROFILE="${DEMO_PROFILE:-minimal}"

die() { echo "error: $*" >&2; exit 1; }

# Optional Honeycomb settings may live in the repo's .env. Read just those two keys (the file is
# not sourced) so the rest of .env never reaches the compose environment.
for key in HONEYCOMB_API_KEY HONEYCOMB_API_URL; do
  if [[ -z "${!key:-}" && -f "$REPO_DIR/.env" ]]; then
    value="$(grep -E "^${key}=" "$REPO_DIR/.env" | tail -n 1 | cut -d= -f2- | sed -e 's/^["'"'"']//' -e 's/["'"'"']$//' || true)"
    if [[ -n "$value" ]]; then export "$key=$value"; fi
  fi
done

[[ -d "$DEMO_DIR/.git" ]] || die "demo clone missing. Run: git clone --depth 1 --branch $PINNED_TAG https://github.com/open-telemetry/opentelemetry-demo $DEMO_DIR"
actual_tag="$(git -C "$DEMO_DIR" describe --tags --exact-match 2>/dev/null || true)"
[[ "$actual_tag" == "$PINNED_TAG" ]] || die "demo clone is at '${actual_tag:-no tag}', expected $PINNED_TAG"
command -v docker >/dev/null || die "docker not found"

mkdir -p "$REPO_DIR/captures"

# Pin the image tags to the release (upstream's .env says DEMO_VERSION=latest) and tell the
# collector and compose where our files are. Shell variables take precedence over the demo's .env.
export DEMO_VERSION="$PINNED_TAG"
export IMAGE_VERSION="$PINNED_TAG"
export INCIDENT_AGENT_CAPTURES_DIR="$REPO_DIR/captures"
export INCIDENT_AGENT_INFRA_DIR="$INFRA_DIR"
export OTEL_COLLECTOR_CONFIG_EXTRAS="$INFRA_DIR/collector-config.yaml"

files=(-f "$DEMO_DIR/compose.yaml")
case "$PROFILE" in
  minimal) ;;
  full) files+=(-f "$DEMO_DIR/compose.full.yaml") ;;
  *) die "DEMO_PROFILE must be minimal or full" ;;
esac
files+=(-f "$INFRA_DIR/docker-compose.override.yaml")
if [[ -n "${HONEYCOMB_API_KEY:-}" ]]; then
  [[ "$PROFILE" == minimal ]] || die "Honeycomb export is only wired for DEMO_PROFILE=minimal"
  files+=(-f "$INFRA_DIR/docker-compose.honeycomb.yaml")
  echo "HONEYCOMB_API_KEY is set: traces will also be exported to Honeycomb." >&2
fi

compose() { docker compose -p "$PROJECT" --project-directory "$DEMO_DIR" "${files[@]}" "$@"; }

check_ports() {
  command -v lsof >/dev/null || return 0
  local port
  for port in "${ENVOY_PORT:-8080}" "${ENVOY_ADMIN_PORT:-10000}" "${INCIDENT_AGENT_OFREP_PORT:-18016}"; do
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
      die "host port $port is already in use (set ENVOY_PORT / ENVOY_ADMIN_PORT / INCIDENT_AGENT_OFREP_PORT to move it)"
    fi
  done
}

cmd="${1:-}"
[[ $# -gt 0 ]] && shift
case "$cmd" in
  pull)
    compose pull
    ;;
  up)
    # Only start if our own stack is not already up; never touches containers from other projects.
    if [[ -z "$(compose ps -q 2>/dev/null)" ]]; then check_ports; fi
    compose up -d --no-build --pull missing
    echo
    echo "Storefront:     http://localhost:${ENVOY_PORT:-8080}"
    echo "Feature flags:  http://localhost:${ENVOY_PORT:-8080}/feature/"
    echo "Load generator: http://localhost:${ENVOY_PORT:-8080}/loadgen/"
    echo "Traces file:    $REPO_DIR/captures/traces.jsonl (appears within ~30s of start-up)"
    ;;
  down)
    compose down
    ;;
  status)
    compose ps
    f="$REPO_DIR/captures/traces.jsonl"
    if [[ -f "$f" ]]; then
      before="$(wc -c <"$f" | tr -d ' ')"
      sleep 5
      after="$(wc -c <"$f" | tr -d ' ')"
      echo "captures/traces.jsonl: $after bytes (+$((after - before)) in 5s)"
    else
      echo "captures/traces.jsonl does not exist yet"
    fi
    ;;
  logs)
    compose logs -f --tail=100 "$@"
    ;;
  restart)
    [[ $# -gt 0 ]] || die "usage: demo.sh restart <service>..."
    compose restart "$@"
    ;;
  config)
    compose config
    ;;
  *)
    sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
