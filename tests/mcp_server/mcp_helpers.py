"""Small helpers shared by the MCP tool tests."""


def approx_tokens(text: str) -> int:
    return len(text) // 4
