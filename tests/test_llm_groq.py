import json
from types import SimpleNamespace

import httpx
import pytest

from app.agent.factory import build_llm
from app.agent.llm_groq import GroqLLM, LLMProviderError, parse_response, to_openai_messages, to_openai_tools

TOOLS = [{"name": "get_business_info", "description": "Lit une info", "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}}}]


def _llm(handler) -> GroqLLM:
    return GroqLLM("gsk_secret", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_outils_traduits_au_format_openai():
    out = to_openai_tools(TOOLS)
    assert out[0]["type"] == "function"
    assert out[0]["function"]["name"] == "get_business_info"
    assert out[0]["function"]["parameters"]["type"] == "object"


def test_historique_d_une_boucle_d_outils_traduit():
    messages = [
        {"role": "user", "content": "Vous ouvrez quand ?"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Je vérifie."},
            {"type": "tool_use", "id": "c1", "name": "get_business_info", "input": {"topic": "horaires"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "{\"ok\": true}", "is_error": False},
            {"type": "tool_result", "tool_use_id": "c2", "content": "boom", "is_error": True},
        ]},
    ]
    out = to_openai_messages("SYS", messages)
    assert out[0] == {"role": "system", "content": "SYS"}
    assert out[1] == {"role": "user", "content": "Vous ouvrez quand ?"}
    assert out[2]["role"] == "assistant" and out[2]["content"] == "Je vérifie."
    call = out[2]["tool_calls"][0]
    assert call["id"] == "c1" and json.loads(call["function"]["arguments"]) == {"topic": "horaires"}
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "{\"ok\": true}"}
    assert out[4]["role"] == "tool" and out[4]["content"].startswith("Erreur : ")


def test_reponse_avec_appel_d_outil():
    data = {
        "choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
            {"id": "abc", "type": "function", "function": {"name": "escalate_to_human", "arguments": "{\"category\": \"plainte\"}"}}]}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 18},
    }
    r = parse_response(data)
    assert r.stop_reason == "tool_use" and r.text == ""
    assert r.tool_calls[0].name == "escalate_to_human" and r.tool_calls[0].input == {"category": "plainte"}
    assert r.content_blocks == [{"type": "tool_use", "id": "abc", "name": "escalate_to_human", "input": {"category": "plainte"}}]
    assert (r.input_tokens, r.output_tokens) == (120, 18)


def test_reponse_texte():
    r = parse_response({"choices": [{"finish_reason": "stop", "message": {"content": " Bonjour ! "}}]})
    assert r.text == "Bonjour !" and r.tool_calls == [] and r.stop_reason == "end_turn"
    assert r.content_blocks == [{"type": "text", "text": "Bonjour !"}]


def test_arguments_invalides_ne_font_pas_planter():
    data = {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
        {"id": "x", "function": {"name": "get_business_info", "arguments": "{pas du json"}}]}}]}
    assert parse_response(data).tool_calls[0].input == {}


def test_reponse_inattendue():
    with pytest.raises(LLMProviderError):
        parse_response({"oops": 1})


def test_requete_envoyee():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"], seen["body"] = str(request.url), request.headers["authorization"], json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}], "usage": {}})

    reply = _llm(handler).create(model="m", system="S", tools=TOOLS, messages=[{"role": "user", "content": "salut"}], max_tokens=1000)
    assert reply.text == "ok"
    assert seen["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert seen["auth"] == "Bearer gsk_secret"
    assert seen["body"]["model"] == "m" and seen["body"]["tool_choice"] == "auto"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "S"}


def test_erreur_du_fournisseur_sans_fuite_de_la_cle():
    def handler(request):
        return httpx.Response(429, headers={"retry-after": "12"}, json={"error": {"message": "Rate limit reached"}})

    with pytest.raises(LLMProviderError) as e:
        _llm(handler).create(model="m", system="S", tools=[], messages=[{"role": "user", "content": "x"}], max_tokens=10)
    assert "429" in str(e.value) and "Rate limit" in str(e.value) and "12" in str(e.value)
    assert "gsk_secret" not in str(e.value)


def _settings(**kw):
    base = dict(llm_provider="groq", app_env="development", anthropic_api_key="", groq_api_key="gsk_x", groq_base_url="https://api.groq.com/openai/v1")
    return SimpleNamespace(**{**base, **kw})


def test_groq_interdit_en_production():
    with pytest.raises(SystemExit):
        build_llm(_settings(app_env="production"))


def test_groq_autorise_en_developpement():
    assert isinstance(build_llm(_settings()), GroqLLM)


def test_cles_manquantes_refusees():
    with pytest.raises(SystemExit):
        build_llm(_settings(groq_api_key=""))
    with pytest.raises(SystemExit):
        build_llm(_settings(llm_provider="anthropic", app_env="production"))
