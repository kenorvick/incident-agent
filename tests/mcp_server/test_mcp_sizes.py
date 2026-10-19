"""Token budget: typical calls on the large synthetic dataset stay under ~2k approximate tokens."""

import asyncio

from incident_agent.mcp_server.server import open_session
from incident_agent.mcp_server.sizecheck import TOKEN_BUDGET, approx_tokens, typical_calls


def test_typical_calls_fit_the_budget_through_mcp(big):
    calls = typical_calls(big)
    assert {tool for _, tool, _ in calls} == {
        "describe_dataset",
        "run_query",
        "find_outliers",
        "get_trace",
        "compare_windows",
    }

    async def main():
        out = []
        async with open_session("big", backend=big) as session:
            for label, tool, args in calls:
                res = await session.call_tool(tool, args)
                out.append((label, res.is_error, "\n".join(c.text for c in res.content)))
        return out

    sizes = {}
    for label, is_error, text in asyncio.run(main()):
        assert not is_error, (label, text)
        sizes[label] = approx_tokens(text)
    too_big = {k: v for k, v in sizes.items() if v >= TOKEN_BUDGET}
    assert not too_big, f"over {TOKEN_BUDGET} approx tokens: {too_big}"
    assert max(sizes.values()) > 100  # the calls returned real content


def test_worst_case_arguments_are_still_bounded(big, call):
    for tool, args in [
        ("describe_dataset", {"max_fields": 200}),
        ("run_query", {"group_by": ["app.user.id", "app.tenant.id"], "limit": 50}),
        (
            "find_outliers",
            {
                "selection": [{"field": "error", "value": True}],
                "max_attributes": 20,
                "max_values": 8,
            },
        ),
    ]:
        text, err = call(big, tool, **args)
        assert not err and approx_tokens(text) < 3000, (tool, approx_tokens(text))
