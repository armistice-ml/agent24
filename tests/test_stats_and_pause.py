import pytest

from scripts.set_tenant_config import set_tenant_config
from tests.conftest import login
from tests.fakes import FakeLLM, FakeSender, text_reply
from tests.helpers import conv_ids, drain, inbound, sql

CONFIG = {"tools_enabled": ["get_business_info", "escalate_to_human"], "business_info": {"horaires": "8h-20h"}}


@pytest.fixture
def vigo(make_tenant, make_user, owner_engine):
    t, _ = make_tenant("vigo", "P1")
    set_tenant_config(owner_engine, t, CONFIG)
    make_user(t, "boss@vigo.bj", "owner")
    make_user(t, "mgr@vigo.bj", "manager")
    return t


def test_overview_counts_and_response_time(client, vigo):
    inbound("P1", "U1", "Bonjour", "m1")
    inbound("P1", "U2", "Salut", "m2")
    drain(FakeLLM(text_reply("Bonjour !"), text_reply("Salut !")), FakeSender())
    login(client, "boss@vigo.bj")
    stats = client.get("/api/stats/overview").json()
    today = stats["today"]
    assert (today["messages_in"], today["agent_replies"], today["escalations"]) == (2, 2, 0)
    assert today["avg_response_seconds"] is not None and float(today["avg_response_seconds"]) < 10
    assert today["input_tokens"] == 20 and stats["conversations_by_status"] == {"agent": 2}
    assert stats["yesterday"]["messages_in"] == 0 and stats["timezone"] == "Africa/Porto-Novo"


def test_hourly_activity_uses_the_clients_local_time(client, vigo, owner_engine):
    for i in range(3):
        inbound("P1", f"U{i}", "Bonjour", f"m{i}")
    # place les trois messages à 10 h 15 heure locale (Cotonou), quelle que soit l'heure UTC du test
    sql(
        owner_engine,
        "UPDATE messages SET received_at = (((now() AT TIME ZONE 'Africa/Porto-Novo')::date + time '10:15') "
        "AT TIME ZONE 'Africa/Porto-Novo')",
    )
    login(client, "boss@vigo.bj")
    activity = client.get("/api/stats/activity").json()
    assert len(activity["today"]) == 24 and activity["today"][10] == 3 and sum(activity["today"]) == 3
    assert sum(activity["yesterday"]) == 0


def test_a_pause_silences_the_agent_but_keeps_the_messages(client, vigo, owner_engine):
    login(client, "mgr@vigo.bj")
    assert client.post("/api/agent/pause").json() == {"paused": True}
    inbound("P1", "U1", "Bonjour", "m1")
    llm, sender = FakeLLM(text_reply("Ne devrait pas partir")), FakeSender()
    drain(llm, sender)
    assert llm.calls == [] and sender.sent == []
    assert sql(owner_engine, "SELECT count(*) FROM messages WHERE direction = 'in'")[0][0] == 1
    assert sql(owner_engine, "SELECT status FROM jobs WHERE kind = 'reply'")[0][0] == "done"

    assert client.post("/api/agent/resume").json() == {"paused": False}
    inbound("P1", "U1", "Vous êtes là ?", "m2")
    drain(FakeLLM(text_reply("Oui, je suis là.")), sender)
    assert [t for _, t in sender.sent] == ["Oui, je suis là."]
    types = [r[0] for r in sql(owner_engine, "SELECT type FROM events WHERE type LIKE 'agent.%' ORDER BY id")]
    assert types == ["agent.paused", "agent.resumed"]


def test_the_clients_name_is_fetched_once_and_never_blocks_the_reply(client, vigo):
    inbound("P1", "U1", "Bonjour", "m1")
    sender = FakeSender(profile_name="Aminatou Sèdjro")
    drain(FakeLLM(text_reply("Bonjour Madame.")), sender)
    login(client, "boss@vigo.bj")
    assert client.get("/api/conversations").json()["items"][0]["contact_display"] == "Aminatou Sèdjro"


def test_profile_lookup_failure_does_not_block_the_reply(client, vigo):
    inbound("P1", "U1", "Bonjour", "m1")
    sender = FakeSender(profile_error=True)
    drain(FakeLLM(text_reply("Bonjour.")), sender)
    assert [t for _, t in sender.sent] == ["Bonjour."]
