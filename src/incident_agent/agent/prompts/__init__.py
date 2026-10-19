"""Prompt files for the agent. The system prompt is static so providers can cache it."""

from functools import cache
from pathlib import Path

_DIR = Path(__file__).parent


@cache
def load_system_prompt() -> str:
    """The system prompt. Contains no per-run content; the alert goes in the first user message."""
    return (_DIR / "system.md").read_text(encoding="utf-8").strip()
