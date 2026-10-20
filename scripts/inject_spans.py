"""Plant instruction-like text in telemetry by sending real OTLP/HTTP spans to the collector.

Used by the prompt-injection scenario. The spans travel the same pipeline as everything else
(collector -> file exporter -> flatten), so the payload reaches the model the way an attacker's
text would in production: as an ordinary attribute value on spans, here `user_agent.original`
(the User-Agent header an attacker controls on a public endpoint).

    uv run python scripts/inject_spans.py --scenario injection-ad-failure-useragent
    uv run python scripts/inject_spans.py --scenario <id> --duration 300   # spread over 5 min
    uv run python scripts/inject_spans.py --scenario <id> --dry-run         # print the OTLP JSON

The payload, target service and attribute come from the scenario's `injection` block in
infra/scenarios.yaml, so the fixture's ground truth and the planted text cannot drift apart.

Trace ids are chosen so that the whole-trace sampler keeps them at every sample rate the capture
finaliser may use (see SAMPLE_RATE_LADDER); otherwise the payload could be sampled away.
"""

import argparse
import json
import os
import secrets
import sys
import time
from typing import Any

import httpx
from scenario_manifest import SAMPLE_RATE_LADDER, get_scenario, load_manifest

from incident_agent.flatten import keep_trace

DEFAULT_URL = f"http://localhost:{os.environ.get('ENVOY_PORT', '8080')}/otlp-http/v1/traces"
BATCH_SIZE = 5


def surviving_trace_id() -> str:
    """A random trace id kept by the sampler at every rate in the ladder (about 1 in 192 are)."""
    while True:
        candidate = secrets.token_hex(16)
        if all(keep_trace(candidate, rate) for rate in SAMPLE_RATE_LADDER):
            return candidate


def build_span(
    *, trace_id: str, name: str, route: str, payload: str, field: str, start_ns: int
) -> dict[str, Any]:
    """One erroring server span shaped like a storefront request, carrying the payload."""
    duration_ns = secrets.randbelow(40_000_000) + 15_000_000  # 15-55 ms
    attrs: list[tuple[str, Any]] = [
        ("http.request.method", "GET"),
        ("http.route", route),
        ("url.path", route),
        ("http.response.status_code", 500),
        (field, payload),
    ]

    def kv(key: str, value: Any) -> dict[str, Any]:
        if isinstance(value, bool):
            return {"key": key, "value": {"boolValue": value}}
        if isinstance(value, int):
            return {"key": key, "value": {"intValue": str(value)}}
        return {"key": key, "value": {"stringValue": str(value)}}

    return {
        "traceId": trace_id,
        "spanId": secrets.token_hex(8),
        "name": name,
        "kind": 2,  # SERVER
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(start_ns + duration_ns),
        "attributes": [kv(k, v) for k, v in attrs],
        "status": {"code": 2, "message": "Internal Server Error"},
    }


def build_request(service: str, version: str, spans: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": service}},
                        {
                            "key": "service.namespace",
                            "value": {"stringValue": "opentelemetry-demo"},
                        },
                        {"key": "service.version", "value": {"stringValue": version}},
                    ]
                },
                "scopeSpans": [{"scope": {"name": "inject_spans"}, "spans": spans}],
            }
        ]
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--scenario", required=True, help="scenario id with an `injection` block")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"OTLP/HTTP traces URL ({DEFAULT_URL})")
    parser.add_argument(
        "--count", type=int, help="spans to send (default: manifest `inject.count`)"
    )
    parser.add_argument(
        "--duration", type=float, default=0, help="spread batches over N seconds (default: at once)"
    )
    parser.add_argument("--dry-run", action="store_true", help="print the request, send nothing")
    args = parser.parse_args()

    manifest = load_manifest()
    scenario = get_scenario(manifest, args.scenario)
    injection, plan = scenario.get("injection"), scenario.get("inject")
    if not injection or not plan:
        print(f"error: {args.scenario} has no injection/inject block", file=sys.stderr)
        return 1
    count = args.count or int(plan.get("count", 40))
    batches = [min(BATCH_SIZE, count - i) for i in range(0, count, BATCH_SIZE)]
    pause = args.duration / max(len(batches) - 1, 1) if args.duration else 0

    sent = 0
    with httpx.Client(timeout=10) as client:
        for index, size in enumerate(batches):
            spans = [
                build_span(
                    trace_id=surviving_trace_id(),
                    name=plan["span_name"],
                    route=plan["http_route"],
                    payload=injection["payload"],
                    field=injection["field"],
                    start_ns=time.time_ns(),
                )
                for _ in range(size)
            ]
            body = build_request(injection["service"], str(manifest["demo_version"]), spans)
            if args.dry_run:
                print(json.dumps(body, indent=2))
                return 0
            try:
                response = client.post(args.url, json=body)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                print(f"error: posting to {args.url} failed: {exc}", file=sys.stderr)
                return 1
            sent += size
            if index < len(batches) - 1 and pause:
                time.sleep(pause)
    print(f"sent {sent} spans for service {injection['service']} to {args.url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
