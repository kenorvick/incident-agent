"""Run typical MCP tool calls against a dataset and print approximate tokens per call.

    uv run python scripts/tool_sizes.py payment-failure-50pct
    uv run python scripts/tool_sizes.py fixtures/payment-failure-50pct --show

The argument is a scenario id (looked up under `fixtures/`) or a fixture directory / events file.
Calls go through a real in-process MCP session. Approximate tokens = characters / 4; the target is
under 2000 per call. Also prints the size of the tool definitions (re-sent on every model turn).
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from incident_agent.backends.duckdb_backend import DuckDBBackend
from incident_agent.mcp_server.server import open_session
from incident_agent.mcp_server.sizecheck import TOKEN_BUDGET, approx_tokens, typical_calls


def load_backend(target: str) -> DuckDBBackend:
    path = Path(target)
    if path.is_file():
        return DuckDBBackend(path)
    if path.is_dir():
        for name in ("events.jsonl.gz", "events.jsonl"):
            if (path / name).is_file():
                return DuckDBBackend(path / name)
    from incident_agent.backends import default_fixtures_dir

    return DuckDBBackend.from_fixture(default_fixtures_dir(), target)


def text_of(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


async def main_async(target: str, show: bool) -> int:
    backend = load_backend(target)
    calls = typical_calls(backend)
    rows = []
    async with open_session(target, backend=backend) as session:
        listing = await session.list_tools()
        defs = {
            t.name: approx_tokens(json.dumps(t.model_dump(by_alias=True, exclude_none=True)))
            for t in listing.tools
        }
        for label, tool, args in calls:
            result = await session.call_tool(tool, args)
            text = text_of(result)
            rows.append((label, tool, len(text), approx_tokens(text), result.is_error, text))
    width = max(len(r[0]) for r in rows)
    print(f"{'call':<{width}}  {'tool':<16} {'chars':>6} {'~tokens':>8}")
    for label, tool, chars, toks, err, text in rows:
        flag = "  ERROR" if err else ("  OVER BUDGET" if toks >= TOKEN_BUDGET else "")
        print(f"{label:<{width}}  {tool:<16} {chars:>6} {toks:>8}{flag}")
        if show:
            print("-" * 60 + f"\n{text}\n" + "-" * 60)
    print(f"\ntool definitions (sent every turn): {sum(defs.values())} ~tokens total")
    print("  " + ", ".join(f"{n} {t}" for n, t in defs.items()))
    worst = max(r[3] for r in rows)
    print(f"max response: {worst} ~tokens (budget {TOKEN_BUDGET})")
    return 1 if worst >= TOKEN_BUDGET or any(r[4] for r in rows) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("target", help="scenario id, fixture directory, or events file")
    parser.add_argument("--show", action="store_true", help="print each response")
    args = parser.parse_args()
    return asyncio.run(main_async(args.target, args.show))


if __name__ == "__main__":
    sys.exit(main())
