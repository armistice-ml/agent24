import pytest
from sqlalchemy import text

from app.agent.orchestrator import FALLBACK_ESCALATION, FALLBACK_TECHNICAL
from app.channels.messenger import parse_payload
from app.db import tenant_session
from app.services.ingest import ingest
from app.worker import run_once
from scripts.send_test_webhook import build_payload
from scripts.set_tenant_config import set_tenant_config
from tests.fakes import FakeEmail, FakeLLM, FakeSender, text_reply, tool_reply

CONFIG = {
    "tone": {"address": "vouvoiement"},
    "tools_enabled": ["get_business_info", "escalate_to_human"],
    "business_info": {"horaires": "7j/7, 8h-20h", "zones": ["Cotonou", "Abomey-Calavi"]},
}


@pytest.fixture
def vigo(make_tenant, owner_engine):
    tenant_id, _ = make_tenant("vigo", "P1")
    set_tenant_config(owner_engine, tenant_id, CONFIG)
    return tenant_id


def _in(psid, body, mid, kind="text"):
    payload = build_payload("P1", psid, body, mid=mid)
    if kind != "text":
        m = payload["entry"][0]["messaging"][0]["message"]
        m.pop("text")
        m["attachments"] = [{"type": kind, "payload": {"url": "https://cdn.example/x"}}]
    return ingest(parse_payload(payload)[0])


def _drain(llm, sender):
    n = 0
    while run_once(llm, sender.factory(), FakeEmail()):
        n += 1
        assert n < 20
    return n


def _rows(tenant_id, sql):
    with tenant_session(tenant_id) as s:
        return s.execute(text(sql)).all()


def _job_statuses(owner_engine):
    with owner_engine.connect() as c:
        return c.execute(text("SELECT status FROM jobs WHERE kind = 'reply' ORDER BY id")).scalars().all()


def test_reply_uses_tool_result_and_is_fully_traced(vigo, owner_engine):
    _in("U1", "Vous êtes ouverts quand ?", "m1")
    llm = FakeLLM(tool_reply("get_business_info", {"section": "horaires"}), text_reply("Nous sommes ouverts 7j/7, de 8h à 20h."))
    sender = FakeSender()
    _drain(llm, sender)

    assert sender.sent == [("U1", "Nous sommes ouverts 7j/7, de 8h à 20h.")]
    # le résultat de l'outil a bien été renvoyé à Claude
    assert "8h-20h" in llm.calls[1]["messages"][-1]["content"][0]["content"]
    # système + outils exposés
    assert {t["name"] for t in llm.calls[0]["tools"]} == {"get_business_info", "escalate_to_human"}
    assert "JAMAIS inventer" in llm.calls[0]["system"] and "vouvoiement" in llm.calls[0]["system"]
    # traces
    [run] = _rows(vigo, "SELECT outcome, steps, input_tokens FROM agent_runs")
    assert (run.outcome, run.steps, run.input_tokens) == ("replied", 2, 20)
    [call] = _rows(vigo, "SELECT tool_name, status FROM tool_calls")
    assert (call.tool_name, call.status) == ("get_business_info", "ok")
    [out] = _rows(vigo, "SELECT delivery_status, external_id FROM messages WHERE direction = 'out'")
    assert (out.delivery_status, out.external_id) == ("sent", "mid_out_1")
    assert _job_statuses(owner_engine) == ["done"]


def test_burst_of_messages_gets_a_single_reply(vigo, owner_engine):
    _in("U1", "Bonjour", "m1")
    _in("U1", "Je veux un passeport", "m2")
    llm = FakeLLM(text_reply("Bonjour, je vous aide pour votre passeport."))
    sender = FakeSender()
    _drain(llm, sender)
    assert len(llm.calls) == 1 and len(sender.sent) == 1
    assert llm.calls[0]["messages"] == [{"role": "user", "content": "Bonjour\nJe veux un passeport"}]
    assert _job_statuses(owner_engine) == ["done", "done"]


def test_next_message_gets_its_own_reply_with_history(vigo):
    _in("U1", "Bonjour", "m1")
    llm = FakeLLM(text_reply("Bonjour !"), text_reply("Avec plaisir."))
    sender = FakeSender()
    _drain(llm, sender)
    _in("U1", "Merci", "m2")
    _drain(llm, sender)
    assert [t for _, t in sender.sent] == ["Bonjour !", "Avec plaisir."]
    assert [m["role"] for m in llm.calls[1]["messages"]] == ["user", "assistant", "user"]


def test_escalation_hands_over_and_silences_the_agent(vigo, owner_engine):
    _in("U1", "Je veux porter plainte", "m1")
    llm = FakeLLM(
        tool_reply("escalate_to_human", {"reason": "plainte", "summary": "Le client veut porter plainte."}),
        text_reply("Je transmets votre demande à l'équipe, qui vous répondra rapidement."),
    )
    sender = FakeSender()
    _drain(llm, sender)
    assert len(sender.sent) == 1
    assert _rows(vigo, "SELECT status FROM conversations")[0].status == "human"
    assert _rows(vigo, "SELECT outcome FROM agent_runs")[0].outcome == "escalated"

    _in("U1", "Allô ?", "m2")                     # l'agent reste silencieux
    _drain(llm, sender)
    assert len(llm.calls) == 2 and len(sender.sent) == 1
    assert _rows(vigo, "SELECT count(*) AS n FROM messages WHERE direction = 'in'")[0].n == 2


def test_tool_refuses_unexpected_tenant_id_parameter(vigo):
    _in("U1", "Bonjour", "m1")
    llm = FakeLLM(
        tool_reply("escalate_to_human", {"reason": "r", "summary": "s", "tenant_id": "00000000-0000-0000-0000-000000000000"}),
        text_reply("Un instant."),
    )
    _drain(llm, FakeSender())
    [call] = _rows(vigo, "SELECT status, error FROM tool_calls")
    assert call.status == "error" and "tenant_id" in call.error
    assert _rows(vigo, "SELECT status FROM conversations")[0].status == "agent"  # aucun effet


def test_disabled_tool_is_neither_exposed_nor_executable(make_tenant, owner_engine):
    tenant_id, _ = make_tenant("v2", "P1")
    set_tenant_config(owner_engine, tenant_id, {**CONFIG, "tools_enabled": ["get_business_info"]})
    _in("U1", "Bonjour", "m1")
    llm = FakeLLM(tool_reply("escalate_to_human", {"reason": "r", "summary": "s"}), text_reply("Un instant."))
    _drain(llm, FakeSender())
    assert [t["name"] for t in llm.calls[0]["tools"]] == ["get_business_info"]
    [call] = _rows(tenant_id, "SELECT status, error FROM tool_calls")
    assert call.status == "error" and "non disponible" in call.error
    assert _rows(tenant_id, "SELECT status FROM conversations")[0].status == "agent"


def test_step_limit_escalates_instead_of_staying_silent(make_tenant, owner_engine):
    tenant_id, _ = make_tenant("v3", "P1")
    set_tenant_config(owner_engine, tenant_id, {**CONFIG, "agent": {"max_steps": 3}})
    _in("U1", "Bonjour", "m1")
    llm = FakeLLM(tool_reply("get_business_info", {}), repeat_last=True)
    sender = FakeSender()
    _drain(llm, sender)
    assert len(llm.calls) == 3
    assert sender.sent == [("U1", FALLBACK_ESCALATION)]
    assert _rows(tenant_id, "SELECT status FROM conversations")[0].status == "human"


def test_transient_send_failure_is_retried_without_calling_claude_again(vigo, owner_engine):
    _in("U1", "Bonjour", "m1")
    llm = FakeLLM(text_reply("Bonjour !"))
    sender = FakeSender(fail_times=1, permanent=False)
    _drain(llm, sender)
    assert sender.sent == [] and _job_statuses(owner_engine) == ["pending"]
    assert _rows(vigo, "SELECT delivery_status FROM messages WHERE direction='out'")[0].delivery_status == "pending"

    with owner_engine.begin() as c:
        c.execute(text("UPDATE jobs SET run_after = now()"))
    _drain(llm, sender)
    assert len(llm.calls) == 1                     # la réponse existait déjà : pas de nouvel appel
    assert sender.sent == [("U1", "Bonjour !")] and _job_statuses(owner_engine) == ["done"]
    assert _rows(vigo, "SELECT delivery_status FROM messages WHERE direction='out'")[0].delivery_status == "sent"


def test_permanent_send_failure_marks_failed_and_hands_over(vigo, owner_engine):
    _in("U1", "Bonjour", "m1")
    sender = FakeSender(fail_times=99, permanent=True)
    _drain(FakeLLM(text_reply("Bonjour !")), sender)
    assert _job_statuses(owner_engine) == ["failed"]
    statuses = {r.delivery_status for r in _rows(vigo, "SELECT delivery_status FROM messages WHERE direction='out'")}
    assert statuses == {"failed"}
    assert _rows(vigo, "SELECT status FROM conversations")[0].status == "human"


def test_llm_outage_ends_with_honest_message_and_handover(vigo, owner_engine):
    _in("U1", "Bonjour", "m1")
    llm = FakeLLM(error=RuntimeError("API indisponible"))
    sender = FakeSender()
    _drain(llm, sender)
    with owner_engine.begin() as c:
        c.execute(text("UPDATE jobs SET run_after = now(), max_attempts = 2"))
    _drain(llm, sender)
    assert _job_statuses(owner_engine) == ["failed"]
    assert sender.sent == [("U1", FALLBACK_TECHNICAL)]
    assert _rows(vigo, "SELECT status FROM conversations")[0].status == "human"


def test_voice_message_is_described_not_invented(vigo):
    _in("U1", "", "m1", kind="audio")
    llm = FakeLLM(text_reply("Pouvez-vous m'écrire votre demande ?"))
    _drain(llm, FakeSender())
    assert "message vocal" in llm.calls[0]["messages"][0]["content"]


def test_message_arriving_during_processing_gets_its_own_reply(vigo, owner_engine):
    _in("U1", "Bonjour", "m1")
    llm = FakeLLM(text_reply("Bonjour !"), text_reply("Je regarde cela."),
                  on_first_call=lambda: _in("U1", "Et pour le passeport ?", "m2"))
    sender = FakeSender()
    _drain(llm, sender)
    assert [t for _, t in sender.sent] == ["Bonjour !", "Je regarde cela."]
    assert [m["role"] for m in llm.calls[1]["messages"]] == ["user", "assistant", "user"]
    assert llm.calls[1]["messages"][-1]["content"] == "Et pour le passeport ?"
    assert _job_statuses(owner_engine) == ["done", "done"]
