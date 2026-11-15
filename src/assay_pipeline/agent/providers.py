"""Model providers with native tool calling (Tech Spec D-8): Claude via the Anthropic SDK
(Messages API) and GPT-6.1 Sol via the OpenAI SDK (Responses API), each offering one tool,
`query(sql)`.

Every response is handled as the plain dict the SDK serialises it to, validated against the
SDK's own type before it is read, so a live turn and a replayed recorded turn take the same
path (AGT-07). A recording is the list of those dicts, one per model turn.

Claude refusals (`stop_reason == "refusal"`) end the trial as a refusal. Server-side refusal
fallbacks are deliberately not enabled: a fallback would let another model answer under this
model's name and corrupt per-model attack results (decision D-30).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

QUERY_TOOL_NAME = "query"
QUERY_TOOL_DESCRIPTION = (
    "Run one read-only SQL SELECT over the approved views and get back at most 50 rows "
    "plus the total row count. Only the approved views listed in your instructions exist."
)
QUERY_SCHEMA = {
    "type": "object",
    "properties": {"sql": {"type": "string", "description": "One SQL SELECT statement."}},
    "required": ["sql"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any] | None  # None when the arguments are not valid JSON
    raw_arguments: str


@dataclass(frozen=True)
class Turn:
    text: str
    tool_calls: tuple[ToolCall, ...]
    stop: str  # "tool_use" | "end" | "refusal" | "max_tokens"
    tokens_in: int  # every input token, cached ones included
    tokens_out: int
    raw: dict[str, Any]
    cache_write_tokens: int = 0  # of tokens_in: written to the prompt cache (Anthropic 1.25x)
    cache_read_tokens: int = 0  # of tokens_in: read from the prompt cache (Anthropic 0.1x)


class Provider(Protocol):
    name: str

    def start(self, system: str, question: str) -> None: ...
    def next_turn(self, final: bool = False) -> Turn: ...
    def add_results(self, results: Sequence[tuple[ToolCall, str]]) -> None: ...


Create = Callable[..., dict[str, Any]]


class ProviderError(RuntimeError):
    """The provider failed after the SDK's own retries (network, API error, a failed response).
    The trial ends with EvalKit's `stop_reason: transport` instead of crashing."""


def _parse_args(raw: Any) -> tuple[dict[str, Any] | None, str]:
    if isinstance(raw, dict):
        return raw, json.dumps(raw, sort_keys=True)
    text = raw if isinstance(raw, str) else json.dumps(raw)
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None, text
    return (value if isinstance(value, dict) else None), text


# ---- Claude (Anthropic Messages API) ----


@dataclass
class AnthropicProvider:
    model: str
    create: Create
    max_tokens: int = 16000
    effort: str = "high"
    name: str = "claude"
    _system: str = ""
    _messages: list[dict[str, Any]] = field(default_factory=list)

    def tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": QUERY_TOOL_NAME,
                "description": QUERY_TOOL_DESCRIPTION,
                "input_schema": QUERY_SCHEMA,
                "strict": True,
            }
        ]

    def start(self, system: str, question: str) -> None:
        self._system = system
        self._messages = [{"role": "user", "content": question}]

    def next_turn(self, final: bool = False) -> Turn:
        """`final`: the step budget is spent, so the model must answer; no tool can be called."""
        raw = self.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[{"type": "text", "text": self._system, "cache_control": {"type": "ephemeral"}}],
            tools=self.tools(),
            tool_choice={"type": "none"} if final else {"type": "auto"},
            output_config={"effort": self.effort},
            messages=list(self._messages),  # a snapshot: later turns never alter this request
        )
        return self.read(raw)

    def read(self, raw: dict[str, Any]) -> Turn:
        from anthropic.types import Message

        msg = Message.model_validate(raw)  # a recording must have the real response shape
        text = "".join(b.text for b in msg.content if b.type == "text")
        calls = []
        for b in msg.content:
            if b.type == "tool_use":
                args, raw_args = _parse_args(b.input)
                calls.append(ToolCall(b.id, b.name, args, raw_args))
        stop = {
            "tool_use": "tool_use",
            "refusal": "refusal",
            "max_tokens": "max_tokens",
        }.get(msg.stop_reason or "", "end")
        if stop == "tool_use" and not calls:
            stop = "end"
        # thinking and tool_use blocks go back unchanged, as the API requires
        self._messages.append({"role": "assistant", "content": raw["content"]})
        u = msg.usage
        write, read = u.cache_creation_input_tokens or 0, u.cache_read_input_tokens or 0
        # input_tokens counts only the uncached part; cached input is billed too (S3.3 verifier)
        return Turn(text, tuple(calls), stop, u.input_tokens + write + read, u.output_tokens, raw,
                    cache_write_tokens=write, cache_read_tokens=read)  # fmt: skip

    def add_results(self, results: Sequence[tuple[ToolCall, str]]) -> None:
        self._messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": call.call_id, "content": text}
                    for call, text in results
                ],  # all results of one turn in one message
            }
        )


# ---- GPT-6.1 Sol (OpenAI Responses API) ----


@dataclass
class OpenAIProvider:
    model: str
    create: Create
    max_output_tokens: int = 16000
    effort: str = "high"
    name: str = "sol"
    _system: str = ""
    _input: list[dict[str, Any]] = field(default_factory=list)

    def tools(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": QUERY_TOOL_NAME,
                "description": QUERY_TOOL_DESCRIPTION,
                "parameters": QUERY_SCHEMA,
                "strict": True,
            }
        ]

    def start(self, system: str, question: str) -> None:
        self._system = system
        self._input = [{"role": "user", "content": question}]

    def next_turn(self, final: bool = False) -> Turn:
        """`final`: the step budget is spent, so the model must answer; no tool can be called."""
        raw = self.create(
            model=self.model,
            instructions=self._system,
            input=list(self._input),  # a snapshot: later turns never alter this request
            tools=self.tools(),
            tool_choice="none" if final else "auto",
            reasoning={"effort": self.effort},
            max_output_tokens=self.max_output_tokens,
        )
        return self.read(raw)

    def read(self, raw: dict[str, Any]) -> Turn:
        from openai.types.responses import Response

        resp = Response.model_validate(raw)
        if resp.status == "failed" or resp.error is not None:
            detail = resp.error.message if resp.error else "status failed"
            raise ProviderError(f"openai: the response failed: {detail}")
        texts: list[str] = []
        refused = False
        calls = []
        for item in resp.output:
            if item.type == "message":
                for part in item.content:
                    if part.type == "output_text":
                        texts.append(part.text)
                    elif part.type == "refusal":
                        refused = True
                        texts.append(part.refusal)
            elif item.type == "function_call":
                args, raw_args = _parse_args(item.arguments)
                calls.append(ToolCall(item.call_id, item.name, args, raw_args))
        incomplete = resp.incomplete_details.reason if resp.incomplete_details else None
        if refused or incomplete == "content_filter":
            stop = "refusal"
        elif incomplete == "max_output_tokens":
            stop = "max_tokens"
        elif calls:
            stop = "tool_use"
        else:
            stop = "end"
        self._input.extend(raw["output"])  # reasoning and calls go back as given
        usage = resp.usage
        return Turn(
            "".join(texts),
            tuple(calls),
            stop,
            usage.input_tokens if usage else 0,
            usage.output_tokens if usage else 0,
            raw,
        )

    def add_results(self, results: Sequence[tuple[ToolCall, str]]) -> None:
        for call, text in results:
            self._input.append(
                {"type": "function_call_output", "call_id": call.call_id, "output": text}
            )


# ---- live clients and replay ----


def anthropic_create(**kwargs: Any) -> dict[str, Any]:
    import anthropic

    try:
        return anthropic.Anthropic().messages.create(**kwargs).to_dict()
    except anthropic.APIError as exc:  # the SDK has already retried 429, 5xx and network errors
        raise ProviderError(f"anthropic: {type(exc).__name__}: {exc}") from exc


def openai_create(**kwargs: Any) -> dict[str, Any]:
    import openai

    try:
        raw = openai.OpenAI().responses.create(**kwargs).to_dict()
    except openai.APIError as exc:
        raise ProviderError(f"openai: {type(exc).__name__}: {exc}") from exc
    return scrub_openai(raw)


def scrub_openai(raw: dict[str, Any]) -> dict[str, Any]:
    """The Responses API echoes the request's `instructions` (the whole system prompt, prompt
    canary included) and `tools` in every response. Drop them as the response arrives, so no
    recording, log or leak scan ever holds text the model did not write."""
    return {**raw, "instructions": None, "tools": []}


class Replay:
    """A `create` that returns recorded responses in order and records the requests made."""

    def __init__(self, responses: Sequence[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    @property
    def remaining(self) -> int:
        return len(self._responses)

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        if not self._responses:
            raise RuntimeError("the recording has no more responses: it does not match this run")
        return self._responses.pop(0)


class Recorder:
    """Wraps a live `create` and keeps every response dict, for `assay ask --record`."""

    def __init__(self, create: Create) -> None:
        self._create = create
        self.responses: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        raw = self._create(**kwargs)
        self.responses.append(raw)
        return raw
