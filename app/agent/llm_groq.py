"""Adaptateur pour les API de type « OpenAI chat completions » (Groq). DÉVELOPPEMENT UNIQUEMENT.

Le reste du code parle le format Anthropic (blocs text / tool_use / tool_result). Cet adaptateur traduit
dans les deux sens, pour que l'orchestrateur et les outils n'aient aucune modification à subir.

Limites assumées : pas de cache de prompt, et les modèles ne sont pas Claude. Ce fournisseur valide la
mécanique (file, outils, escalade, envoi), PAS la qualité ni la fiabilité de l'agent.
"""
import json
import logging
from dataclasses import dataclass, field

import httpx

from app.agent.llm import LLMReply, ToolCall

log = logging.getLogger(__name__)

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
_FINISH_TO_STOP = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens"}


class LLMProviderError(Exception):
    """Erreur renvoyée par le fournisseur. Le message ne contient jamais la clé d'API."""


def to_openai_tools(tools: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t["input_schema"]},
        }
        for t in tools
    ]


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content if b.get("type") == "text")


def to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        role, content = m["role"], m["content"]
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "assistant":
            calls = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)},
                }
                for b in content
                if b.get("type") == "tool_use"
            ]
            msg: dict = {"role": "assistant", "content": _text_of(content)}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        else:  # tour « user » : résultats d'outils et/ou texte
            for b in content:
                if b.get("type") == "tool_result":
                    body = b.get("content")
                    body = body if isinstance(body, str) else _text_of(body or [])
                    if b.get("is_error"):
                        body = "Erreur : " + body
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": body})
            text = _text_of([b for b in content if b.get("type") == "text"])
            if text:
                out.append({"role": "user", "content": text})
    return out


def parse_response(data: dict) -> LLMReply:
    try:
        choice = data["choices"][0]
        msg = choice["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMProviderError("réponse inattendue du fournisseur") from e

    text = (msg.get("content") or "").strip()
    calls: list[ToolCall] = []
    blocks: list[dict] = []
    if text:
        blocks.append({"type": "text", "text": text})
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        name, raw = fn.get("name", ""), fn.get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            if not isinstance(args, dict):
                args = {}
        except (ValueError, TypeError):
            # Les petits modèles produisent parfois des arguments invalides : l'outil les refusera
            # et le modèle verra l'erreur, comme avec Claude.
            log.warning("arguments d'outil invalides pour %s", name)
            args = {}
        call_id = tc.get("id") or f"call_{i}"
        calls.append(ToolCall(call_id, name, args))
        blocks.append({"type": "tool_use", "id": call_id, "name": name, "input": args})

    usage = data.get("usage") or {}
    finish = choice.get("finish_reason")
    return LLMReply(
        text=text,
        tool_calls=calls,
        content_blocks=blocks,
        stop_reason=_FINISH_TO_STOP.get(finish, finish),
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
    )


def _error_message(resp: httpx.Response) -> str:
    try:
        return str(resp.json()["error"]["message"])[:300]
    except Exception:
        return resp.text[:200]


@dataclass
class GroqLLM:
    api_key: str = field(repr=False)
    base_url: str = GROQ_BASE_URL
    temperature: float = 0.3
    timeout: float = 60.0
    client: httpx.Client | None = field(default=None, repr=False)

    def __post_init__(self):
        if self.client is None:
            self.client = httpx.Client(timeout=self.timeout)

    def create(self, *, model: str, system: str, tools: list[dict], messages: list[dict], max_tokens: int) -> LLMReply:
        payload: dict = {
            "model": model,
            "messages": to_openai_messages(system, messages),
            "max_tokens": max_tokens,
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = to_openai_tools(tools)
            payload["tool_choice"] = "auto"
        resp = self.client.post(
            f"{self.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=payload,
        )
        if resp.status_code >= 400:
            hint = f" (réessayer dans {resp.headers['retry-after']} s)" if "retry-after" in resp.headers else ""
            raise LLMProviderError(f"HTTP {resp.status_code}: {_error_message(resp)}{hint}")
        return parse_response(resp.json())
