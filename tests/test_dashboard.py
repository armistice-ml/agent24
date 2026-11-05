import pytest
from sqlalchemy import text

from app.db import tenant_session
from scripts.set_tenant_config import set_tenant_config
from tests.conftest import login
from tests.fakes import FakeLLM, FakeSender, text_reply, tool_reply
from tests.helpers import conv_ids, drain, inbound, sql

CONFIG = {
    "tools_enabled": ["get_business_info", "escalate_to_human"],
    "business_info": {"horaires": "7j/7, 8h-20h"},
}
ESCALATE = tool_reply(
    "escalate_to_human",
    {"category": "information_manquante", "reason": "Tarif non renseigné", "summary": "Veut renouveler son passeport et demande le prix."},
)


@pytest.fixture
def vigo(make_tenant, make_user, owner_engine):
    t, _ = make_tenant("vigo", "P1")
    set_tenant_config(owner_engine, t, CONFIG)
    make_user(t, "boss@vigo.bj", "owner", name="Le Dirigeant")
    return t


def _escalated(vigo, owner_engine, psid="U1"):
    """Un client écrit, l'agent escalade. Retourne l'id de la conversation."""
    inbound("P1", psid, "Combien coûte un passeport ?", f"m_{psid}")
    drain(FakeLLM(ESCALATE, text_reply("Je transmets votre demande à l'équipe.")), FakeSender())
    return conv_ids(owner_engine, vigo)[-1]


def test_a_user_never_sees_another_clients_data(client, make_tenant, make_user, owner_engine):
    ta, _ = make_tenant("a", "PA")
    tb, _ = make_tenant("b", "PB")
    make_user(ta, "a@a.bj", "owner")
    make_user(tb, "b@b.bj", "owner")
    inbound("PA", "U1", "message pour A", "m1")
    inbound("PB", "U1", "message pour B", "m2")
    conv_b = conv_ids(owner_engine, tb)[0]

    login(client, "a@a.bj")
    items = client.get("/api/conversations").json()["items"]
    assert [i["last_text"] for i in items] == ["message pour A"]
    for call in (
        client.get(f"/api/conversations/{conv_b}"),
        client.get(f"/api/conversations/{conv_b}/messages"),
        client.post(f"/api/conversations/{conv_b}/read"),
        client.post(f"/api/conversations/{conv_b}/take"),
        client.post(f"/api/conversations/{conv_b}/messages", json={"text": "intrus"}),
    ):
        assert call.status_code == 404
    assert sql(owner_engine, "SELECT status FROM conversations WHERE id = :c", c=conv_b)[0][0] == "agent"


def test_conversation_list_preview_unread_filters_and_search(client, vigo):
    inbound("P1", "U1", "Bonjour pour un passeport", "m1")
    inbound("P1", "U1", "Merci", "m2")
    inbound("P1", "U2", "Vous livrez des colis ?", "m3")
    login(client, "boss@vigo.bj")

    items = client.get("/api/conversations").json()["items"]
    assert [i["last_text"] for i in items] == ["Vous livrez des colis ?", "Merci"]   # plus récent d'abord
    assert [i["unread"] for i in items] == [1, 2]
    assert items[0]["contact_display"].startswith("Client ••")

    conv = items[1]["id"]
    assert client.post(f"/api/conversations/{conv}/read").status_code == 204
    assert client.get("/api/conversations").json()["items"][1]["unread"] == 0

    assert [i["last_text"] for i in client.get("/api/conversations", params={"q": "passeport"}).json()["items"]] == ["Merci"]
    assert client.get("/api/conversations", params={"q": "%"}).json()["items"] == []       # % n'est pas un joker
    assert len(client.get("/api/conversations", params={"channel": "messenger"}).json()["items"]) == 2
    assert client.get("/api/conversations", params={"channel": "whatsapp"}).json()["items"] == []
    assert client.get("/api/conversations", params={"status": "human"}).json()["items"] == []
    assert client.get("/api/conversations", params={"status": "nimporte"}).status_code == 422


def test_messages_are_paginated_in_chronological_order(client, vigo, owner_engine):
    for i in range(5):
        inbound("P1", "U1", f"message {i}", f"m{i}")
    conv = conv_ids(owner_engine, vigo)[0]
    login(client, "boss@vigo.bj")

    p1 = client.get(f"/api/conversations/{conv}/messages", params={"limit": 2}).json()
    assert [m["text"] for m in p1["items"]] == ["message 3", "message 4"] and p1["has_more"]
    p2 = client.get(f"/api/conversations/{conv}/messages", params={"limit": 2, "before": p1["next_before"]}).json()
    assert [m["text"] for m in p2["items"]] == ["message 1", "message 2"] and p2["has_more"]
    p3 = client.get(f"/api/conversations/{conv}/messages", params={"limit": 2, "before": p2["next_before"]}).json()
    assert [m["text"] for m in p3["items"]] == ["message 0"] and not p3["has_more"]


def test_agent_escalation_is_visible_with_reason_and_summary(client, vigo, owner_engine):
    conv = _escalated(vigo, owner_engine)
    login(client, "boss@vigo.bj")

    esc = client.get("/api/escalations").json()
    assert esc["counts"] == {"open": 1, "taken": 0, "resolved": 0}
    [item] = esc["items"]
    assert (item["category"], item["reason"], item["status"]) == ("information_manquante", "Tarif non renseigné", "open")
    assert "passeport" in item["summary"] and item["conversation_id"] == str(conv)

    detail = client.get(f"/api/conversations/{conv}").json()
    assert detail["status"] == "human" and detail["escalation_status"] == "open" and detail["reply_window"]["open"]
    msgs = client.get(f"/api/conversations/{conv}/messages").json()["items"]
    assert [m["author"] for m in msgs] == ["customer", "agent"]


def test_take_then_reply_sends_as_a_human(client, vigo, owner_engine):
    conv = _escalated(vigo, owner_engine)
    login(client, "boss@vigo.bj")

    assert client.post(f"/api/conversations/{conv}/take").status_code == 200
    assert client.get("/api/escalations", params={"status": "open"}).json()["items"] == []
    [taken] = client.get("/api/escalations", params={"status": "handled"}).json()["items"]
    assert taken["status"] == "taken" and taken["taken_by_name"] == "Le Dirigeant"

    r = client.post(f"/api/conversations/{conv}/messages", json={"text": "Bonjour, le passeport coûte ..."})
    assert r.status_code == 201
    assert client.fake_sender.sent == [("U1", "Bonjour, le passeport coûte ...")]
    last = client.get(f"/api/conversations/{conv}/messages").json()["items"][-1]
    assert (last["author"], last["delivery_status"], last["sent_by_name"]) == ("user", "sent", "Le Dirigeant")


def test_reply_is_refused_unless_the_human_took_over(client, vigo):
    inbound("P1", "U1", "Bonjour", "m1")
    login(client, "boss@vigo.bj")
    conv = client.get("/api/conversations").json()["items"][0]["id"]
    r = client.post(f"/api/conversations/{conv}/messages", json={"text": "salut"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_human"
    assert client.fake_sender.sent == []


def test_reply_is_refused_after_the_24h_window(client, vigo, owner_engine):
    conv = _escalated(vigo, owner_engine)
    sql(owner_engine, "UPDATE messages SET received_at = now() - interval '30 hours' WHERE direction = 'in'")
    login(client, "boss@vigo.bj")
    client.post(f"/api/conversations/{conv}/take")
    assert client.get(f"/api/conversations/{conv}").json()["reply_window"]["open"] is False
    r = client.post(f"/api/conversations/{conv}/messages", json={"text": "bonjour"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "window_expired"
    assert client.fake_sender.sent == []


def test_failed_send_is_reported_and_recorded(client, vigo, owner_engine):
    conv = _escalated(vigo, owner_engine)
    login(client, "boss@vigo.bj")
    client.post(f"/api/conversations/{conv}/take")
    client.fake_sender.fail_times, client.fake_sender.permanent = 1, True
    r = client.post(f"/api/conversations/{conv}/messages", json={"text": "bonjour"})
    assert r.status_code == 502 and r.json()["detail"]["code"] == "send_failed"
    assert client.get(f"/api/conversations/{conv}/messages").json()["items"][-1]["delivery_status"] == "failed"


def test_empty_reply_is_rejected(client, vigo, owner_engine):
    conv = _escalated(vigo, owner_engine)
    login(client, "boss@vigo.bj")
    client.post(f"/api/conversations/{conv}/take")
    assert client.post(f"/api/conversations/{conv}/messages", json={"text": "   "}).status_code == 422
    assert client.post(f"/api/conversations/{conv}/messages", json={"text": ""}).status_code == 422


def test_handback_returns_the_conversation_to_the_agent(client, vigo, owner_engine):
    conv = _escalated(vigo, owner_engine)
    login(client, "boss@vigo.bj")
    client.post(f"/api/conversations/{conv}/take")
    assert client.post(f"/api/conversations/{conv}/handback").status_code == 200
    detail = client.get(f"/api/conversations/{conv}").json()
    assert detail["status"] == "agent" and detail["escalation_id"] is None
    assert client.get("/api/escalations", params={"status": "resolved"}).json()["items"][0]["resolution"] == "handed_back"

    inbound("P1", "U1", "Merci, une autre question", "m_next")        # l'agent répond de nouveau
    sender = FakeSender()
    drain(FakeLLM(text_reply("Avec plaisir.")), sender)
    assert [t for _, t in sender.sent] == ["Avec plaisir."]
    assert client.post(f"/api/conversations/{conv}/handback").status_code == 409   # déjà rendue


def test_close_ends_the_conversation_and_the_next_message_opens_a_new_one(client, vigo, owner_engine):
    conv = _escalated(vigo, owner_engine)
    login(client, "boss@vigo.bj")
    assert client.post(f"/api/conversations/{conv}/close").status_code == 200
    assert client.get("/api/conversations").json()["items"] == []                     # masquée par défaut
    assert len(client.get("/api/conversations", params={"status": "closed"}).json()["items"]) == 1
    assert client.post(f"/api/conversations/{conv}/messages", json={"text": "x"}).status_code == 409

    inbound("P1", "U1", "Bonjour, nouvelle demande", "m_new")
    assert len(conv_ids(owner_engine, vigo)) == 2
    assert client.get("/api/escalations", params={"status": "resolved"}).json()["items"][0]["resolution"] == "closed"


def test_escalation_is_idempotent_per_conversation(client, vigo, owner_engine):
    conv = _escalated(vigo, owner_engine)
    from app.services.handoff import create_escalation

    with tenant_session(vigo) as s:
        _, created = create_escalation(s, tenant_id=vigo, conversation_id=conv, category="autre", reason="x", summary="y", source="agent")
    assert created is False
    assert sql(owner_engine, "SELECT count(*) FROM escalations")[0][0] == 1
