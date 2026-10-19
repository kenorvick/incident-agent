"""Provider-neutral model types and the async `ModelClient` protocol.

Everything the agent loop sees is defined here; provider modules translate to and from
their wire formats. Token accounting follows Anthropic's convention: `Usage.input_tokens`
counts only *uncached* input. Use `Usage.total_input_tokens` for the full prompt size.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

StopReason = Literal["end_turn", "tool_use", "max_tokens", "other"]
CostSource = Literal["provider", "estimated"]


# --- errors -----------------------------------------------------------------------------


class ModelError(Exception):
    """Base class for every error raised by the model layer."""


class ModelConfigError(ModelError):
    """Bad or missing configuration (for example a missing API key)."""


class BudgetExceededError(ModelError):
    """Cumulative spend reached the cap; no API call was made."""


class ModelAPIError(ModelError):
    """A provider call failed. `retryable` tells the retry layer whether to try again."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
        attempts: int = 1,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after
        self.attempts = attempts


class ModelAuthError(ModelAPIError):
    """The provider rejected the API key (never retried)."""


# --- content blocks ---------------------------------------------------------------------


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(BaseModel):
    """A tool call requested by the model.

    `parse_error` is set when the provider returned arguments that were not a JSON object;
    `input` is then `{}` and the loop should answer with an error `tool_result`.
    """

    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)
    parse_error: str | None = None


class ToolResultBlock(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False


ContentBlock = Annotated[TextBlock | ToolUseBlock | ToolResultBlock, Field(discriminator="type")]


class Message(BaseModel):
    """One conversation turn.

    Tool results travel in a `user` message. `provider_data` is opaque state the provider
    needs echoed back (reasoning blocks); keep it with the message and do not edit it.
    """

    role: Literal["user", "assistant"]
    content: list[ContentBlock]
    provider_data: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def user(cls, text: str) -> Message:
        return cls(role="user", content=[TextBlock(text=text)])

    @classmethod
    def assistant(cls, text: str) -> Message:
        return cls(role="assistant", content=[TextBlock(text=text)])

    @classmethod
    def tool_results(cls, results: Sequence[ToolResultBlock]) -> Message:
        """One user turn carrying every result for a (possibly parallel) tool-call turn."""
        return cls(role="user", content=list(results))


class ToolSpec(BaseModel):
    """A tool the model may call. `input_schema` is a JSON Schema object."""

    name: str
    description: str
    input_schema: dict[str, Any]


# --- response ---------------------------------------------------------------------------


class Usage(BaseModel):
    input_tokens: int = 0  # uncached input only
    output_tokens: int = 0  # includes reasoning tokens
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0  # informational subset of output_tokens

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


class ModelResponse(BaseModel):
    text: str = ""
    tool_calls: list[ToolUseBlock] = Field(default_factory=list)
    stop_reason: StopReason = "end_turn"
    raw_stop_reason: str | None = None
    usage: Usage = Field(default_factory=Usage)
    cost_usd: float = 0.0
    cost_source: CostSource = "estimated"
    model: str = ""
    provider: str = ""
    latency_ms: float = 0.0
    attempts: int = 1
    request_id: str | None = None
    provider_data: dict[str, Any] = Field(default_factory=dict)

    def to_message(self) -> Message:
        """The assistant `Message` to append to history (carries reasoning state)."""
        blocks: list[ContentBlock] = []
        if self.text:
            blocks.append(TextBlock(text=self.text))
        blocks.extend(self.tool_calls)
        return Message(role="assistant", content=blocks, provider_data=self.provider_data)


@runtime_checkable
class ModelClient(Protocol):
    """Async model access. Implementations must be safe to call concurrently."""

    async def complete(
        self,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        max_tokens: int,
    ) -> ModelResponse:
        """One model turn. `max_tokens` bounds reasoning plus visible output."""
        ...
