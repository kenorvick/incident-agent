"""Model clients: provider-neutral types, OpenRouter and Anthropic implementations, spend guard.

Use `get_model_client()`; it always returns a client wrapped by the spend guard.
"""

from __future__ import annotations

import os
from typing import Any

from dotenv import find_dotenv, load_dotenv

from incident_agent.agent.models.base import (
    BudgetExceededError,
    ContentBlock,
    Message,
    ModelAPIError,
    ModelAuthError,
    ModelClient,
    ModelConfigError,
    ModelError,
    ModelResponse,
    StopReason,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    Usage,
)
from incident_agent.agent.models.budget import (
    REPO_ROOT,
    BudgetedModelClient,
    SpendLedger,
    SpendSummary,
    spend_label,
)

__all__ = [
    "BudgetExceededError",
    "BudgetedModelClient",
    "ContentBlock",
    "Message",
    "ModelAPIError",
    "ModelAuthError",
    "ModelClient",
    "ModelConfigError",
    "ModelError",
    "ModelResponse",
    "SpendLedger",
    "SpendSummary",
    "StopReason",
    "TextBlock",
    "ToolResultBlock",
    "ToolSpec",
    "ToolUseBlock",
    "Usage",
    "get_model_client",
    "spend_label",
]

PROVIDERS = ("openrouter", "anthropic")
_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def get_model_client(
    provider: str | None = None,
    model_id: str | None = None,
    *,
    ledger: SpendLedger | None = None,
    label: str | None = None,
    load_env: bool = True,
    **client_kwargs: Any,
) -> BudgetedModelClient:
    """Build the configured client, wrapped by the spend guard (there is no unwrapped path).

    Reads `MODEL_PROVIDER` (openrouter|anthropic, default openrouter), `MODEL_ID` (default: the
    provider's Sonnet 5.5 ID), `OPENROUTER_API_KEY` / `ANTHROPIC_API_KEY`, `MODEL_EFFORT`
    (low|medium|high|xhigh|max|none, default medium) and `SPEND_CAP_USD` from the environment,
    loading `.env` first. Extra keyword arguments (`http_client`, `retry_policy`, `sleep`,
    `cache_ttl`, ...) go to the provider client.
    """
    if load_env:
        load_dotenv(find_dotenv(usecwd=True) or REPO_ROOT / ".env", override=False)

    provider = (provider or os.environ.get("MODEL_PROVIDER") or "openrouter").strip().lower()
    model_id = (model_id or os.environ.get("MODEL_ID") or "").strip()
    effort = _effort_from_env()

    if provider == "openrouter":
        from incident_agent.agent.models.openrouter import DEFAULT_MODEL, OpenRouterClient

        key = _require_key("OPENROUTER_API_KEY")
        inner: ModelClient = OpenRouterClient(
            key, model_id or DEFAULT_MODEL, effort=effort, **client_kwargs
        )
    elif provider == "anthropic":
        from incident_agent.agent.models.anthropic import DEFAULT_MODEL, AnthropicClient

        if "/" in model_id:
            raise ModelConfigError(
                f"MODEL_ID {model_id!r} looks like an OpenRouter ID. For MODEL_PROVIDER=anthropic "
                f"use the Anthropic API ID, for example {DEFAULT_MODEL!r}."
            )
        key = _require_key("ANTHROPIC_API_KEY")
        inner = AnthropicClient(key, model_id or DEFAULT_MODEL, effort=effort, **client_kwargs)
    else:
        raise ModelConfigError(
            f"MODEL_PROVIDER must be one of {', '.join(PROVIDERS)}; got {provider!r}."
        )
    return BudgetedModelClient(inner, ledger or SpendLedger(), label)


def _require_key(name: str) -> str:
    key = os.environ.get(name, "").strip()
    if not key:
        raise ModelConfigError(f"{name} is not set. Add it to .env (see .env.example).")
    return key


def _effort_from_env() -> str | None:
    raw = os.environ.get("MODEL_EFFORT", "").strip().lower() or "medium"
    if raw in ("none", "off"):
        return None
    if raw not in _EFFORTS:
        raise ModelConfigError(
            f"MODEL_EFFORT must be one of {', '.join(_EFFORTS)} or none; got {raw!r}."
        )
    return raw
