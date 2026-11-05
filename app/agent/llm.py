"""Abstraction du modèle de langage : permet de tester sans appel réseau et de changer de fournisseur."""
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class LLMReply:
    text: str
    tool_calls: list[ToolCall]
    content_blocks: list[dict]      # tel quel, pour rejouer le tour de l'assistant dans l'historique
    stop_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


class LLMClient(Protocol):
    def create(self, *, model: str, system: str, tools: list[dict], messages: list[dict], max_tokens: int) -> LLMReply: ...


@dataclass
class AnthropicLLM:
    api_key: str
    _client: object = field(default=None, repr=False)

    def __post_init__(self):
        import anthropic

        self._client = anthropic.Anthropic(api_key=self.api_key, max_retries=2, timeout=60)

    def create(self, *, model, system, tools, messages, max_tokens) -> LLMReply:
        kwargs: dict = dict(
            model=model,
            max_tokens=max_tokens,
            # La partie stable du prompt est marquée pour le cache (réduit coût et latence).
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
        )
        if tools:
            kwargs["tools"] = tools
        resp = self._client.messages.create(**kwargs)

        text_parts, calls, blocks = [], [], []
        for b in resp.content:
            if b.type == "text":
                text_parts.append(b.text)
                blocks.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                calls.append(ToolCall(id=b.id, name=b.name, input=dict(b.input)))
                blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": dict(b.input)})
        return LLMReply(
            text="".join(text_parts).strip(),
            tool_calls=calls,
            content_blocks=blocks,
            stop_reason=resp.stop_reason,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )
