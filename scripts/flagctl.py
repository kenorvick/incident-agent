"""Read and change the OpenTelemetry Demo's feature flags, and confirm flagd sees the change.

    uv run python scripts/flagctl.py list
    uv run python scripts/flagctl.py status
    uv run python scripts/flagctl.py set paymentFailure=50% adManualGc=on
    uv run python scripts/flagctl.py reset

How it works (read from demo 3.1.0 source, src/flagd-ui):
  * flagd serves flags from src/flagd/demo.flagd.json and re-reads the file when it changes
    (its `fileinfo` poller stats the file every second).
  * The flagd-ui service is the single writer of that file. Its API is reachable through the
    frontend proxy: GET /feature/api/read returns {"flags": {...}} and POST /feature/api/write
    with {"data": {"flags": {...}}} replaces the whole file (write-then-rename).
  * A flag's variant lives in `defaultVariant`, except for flags with a targeting rule of the form
    {"if": [condition, THEN, ELSE]} (only productCatalogFailure): the UI changes THEN, so we do too.
  * After writing, we poll flagd's OFREP endpoint until it evaluates the flag to the new variant,
    so a caller knows the change is live before it records a fault start.

`--via file` edits the flag file on the host directly instead (fallback if flagd-ui is down).
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, NoReturn

import httpx
from scenario_manifest import DEMO_FLAGS_PATH

DEMO_URL = f"http://localhost:{os.environ.get('ENVOY_PORT', '8080')}"
OFREP_URL = f"http://127.0.0.1:{os.environ.get('INCIDENT_AGENT_OFREP_PORT', '18016')}"
RESTING = "off"

Flags = dict[str, dict[str, Any]]


def die(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


# --- flag file model ------------------------------------------------------------------------


def targeting_if(flag: dict[str, Any]) -> list[Any] | None:
    """The [condition, then, else] list of an if/then/else targeting rule, if the flag has one."""
    rule = (flag.get("targeting") or {}).get("if")
    return rule if isinstance(rule, list) and len(rule) == 3 else None


def current_variant(flag: dict[str, Any]) -> str:
    rule = targeting_if(flag)
    return str(rule[1]) if rule else str(flag.get("defaultVariant"))


def with_variant(flag: dict[str, Any], variant: str) -> dict[str, Any]:
    updated = json.loads(json.dumps(flag))
    rule = targeting_if(updated)
    if rule:
        rule[1] = variant
    else:
        updated["defaultVariant"] = variant
    return updated


def evaluation_context(flag: dict[str, Any]) -> dict[str, Any]:
    """Context that makes a targeted flag evaluate its THEN branch (e.g. product_id)."""
    rule = targeting_if(flag)
    try:
        condition = rule[0] if rule else None
        if condition and "==" in condition:
            left, right = condition["=="]
            return {left["var"]: right}
    except (TypeError, KeyError, ValueError):
        pass
    return {}


def apply_changes(flags: Flags, changes: dict[str, str]) -> Flags:
    updated = dict(flags)
    for name, variant in changes.items():
        if name not in flags:
            die(f"unknown flag {name!r}. Known flags: {', '.join(sorted(flags))}")
        variants = flags[name].get("variants", {})
        if variant not in variants:
            die(f"flag {name!r} has no variant {variant!r}. Variants: {', '.join(variants)}")
        updated[name] = with_variant(flags[name], variant)
    return updated


# --- reading and writing --------------------------------------------------------------------


class FlagStore:
    """Reads and writes the flag document through flagd-ui (default) or the host file."""

    def __init__(self, via: str, demo_url: str, client: httpx.Client) -> None:
        self.via, self.demo_url, self.client = via, demo_url, client

    def read(self) -> Flags:
        if self.via == "file":
            return json.loads(DEMO_FLAGS_PATH.read_text())["flags"]
        try:
            response = self.client.get(f"{self.demo_url}/feature/api/read")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            die(
                f"cannot read flags from {self.demo_url}/feature/api/read ({exc}). "
                "Is the demo running (infra/demo.sh up)? Or retry with --via file."
            )
        return response.json()["flags"]

    def write(self, flags: Flags) -> None:
        if self.via == "file":
            document = json.loads(DEMO_FLAGS_PATH.read_text())
            document["flags"] = flags
            tmp = Path(str(DEMO_FLAGS_PATH) + ".tmp")
            tmp.write_text(json.dumps(document, indent=2) + "\n")
            tmp.replace(DEMO_FLAGS_PATH)
            return
        try:
            response = self.client.post(
                f"{self.demo_url}/feature/api/write", json={"data": {"flags": flags}}
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            die(f"flag write failed: {exc}")


def evaluate(
    client: httpx.Client, ofrep_url: str, name: str, context: dict[str, Any]
) -> str | None:
    """Variant flagd currently serves for `name`, or None if it cannot be evaluated."""
    try:
        response = client.post(
            f"{ofrep_url}/ofrep/v1/evaluate/flags/{name}", json={"context": context}
        )
        if response.status_code != 200:
            return None
        return response.json().get("variant")
    except (httpx.HTTPError, ValueError):
        return None


def wait_until_live(
    client: httpx.Client, ofrep_url: str, flags: Flags, expected: dict[str, str], timeout: float
) -> float:
    """Poll flagd until every flag in `expected` evaluates to its variant. Returns seconds taken."""
    started = time.monotonic()
    pending = dict(expected)
    while True:
        for name, variant in list(pending.items()):
            context = evaluation_context(flags[name])
            if evaluate(client, ofrep_url, name, context) == variant:
                del pending[name]
        if not pending:
            return time.monotonic() - started
        if time.monotonic() - started > timeout:
            die(
                f"flagd did not serve {pending} within {timeout:.0f}s "
                f"(OFREP at {ofrep_url}). Check `infra/demo.sh logs flagd flagd-ui`."
            )
        time.sleep(0.5)


# --- commands -------------------------------------------------------------------------------


def parse_assignments(items: list[str]) -> dict[str, str]:
    changes: dict[str, str] = {}
    for item in items:
        name, sep, variant = item.partition("=")
        if not sep or not name or not variant:
            die(f"expected NAME=VARIANT, got {item!r}")
        changes[name] = variant
    return changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default=DEMO_URL, help=f"frontend proxy (default {DEMO_URL})")
    parser.add_argument("--ofrep-url", default=OFREP_URL, help=f"flagd OFREP (default {OFREP_URL})")
    parser.add_argument("--via", choices=["api", "file"], default="api")
    parser.add_argument("--timeout", type=float, default=30, help="seconds to wait for flagd")
    parser.add_argument("--no-wait", action="store_true", help="do not wait for flagd to confirm")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="all flags with their variants and current setting")
    sub.add_parser("status", help="flags that are not at their resting `off` variant")
    sub.add_parser("reset", help="set every flag back to `off`")
    for name in ("set", "verify"):
        p = sub.add_parser(name, help=f"{name} flags (NAME=VARIANT ...)")
        p.add_argument("assignments", nargs="+")
    args = parser.parse_args(argv)

    with httpx.Client(timeout=10) as client:
        store = FlagStore(args.via, args.url.rstrip("/"), client)
        flags = store.read()

        if args.command == "list":
            for name, flag in sorted(flags.items()):
                print(f"{name:32s} {current_variant(flag):8s} {list(flag.get('variants', {}))}")
            return 0

        if args.command == "status":
            current = {n: current_variant(f) for n, f in flags.items()}
            active = {n: v for n, v in current.items() if v != RESTING}
            print(json.dumps(active) if active else "all flags off")
            return 0

        if args.command == "verify":
            expected = parse_assignments(args.assignments)
            elapsed = wait_until_live(client, args.ofrep_url, flags, expected, args.timeout)
            print(f"flagd serves {expected} (confirmed in {elapsed:.1f}s)")
            return 0

        if args.command == "set":
            changes = parse_assignments(args.assignments)
        else:  # reset
            changes = {
                n: RESTING
                for n, f in flags.items()
                if RESTING in f.get("variants", {}) and current_variant(f) != RESTING
            }
            if not changes:
                print("all flags already off")
                # Still confirm flagd agrees, in case the file changed but flagd has not caught up.
                changes = {n: RESTING for n, f in flags.items() if RESTING in f.get("variants", {})}
                wait_until_live(client, args.ofrep_url, flags, changes, args.timeout)
                return 0

        updated = apply_changes(flags, changes)
        store.write(updated)
        if args.no_wait:
            print(f"wrote {changes} (not waiting for flagd)")
            return 0
        elapsed = wait_until_live(client, args.ofrep_url, updated, changes, args.timeout)
        print(f"{args.command}: {changes} (live in flagd after {elapsed:.1f}s)")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
