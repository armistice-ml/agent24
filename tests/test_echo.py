import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.channels.messenger import parse_echoes
from app.main import app
from app.schemas import EchoEvent
from app.services.handoff import register_page_reply
from scripts.send_test_webhook import build_payload, sign
from tests.fakes import FakeLLM, FakeSender, text_reply
from tests.helpers import drain, inbound, sql
from datetime import UTC, datetime


def _echo(mid="m_echo", app_id=None, psid="U1", page="P1", body="Je m'en occupe."):
    return EchoEvent(channel_external_id=page, customer_id=psid, mid=mid, app_id=app_id, text=body, received_at=datetime.now(UTC))


@pytest.fixture
def vigo(make_tenant):
    t, _ = make_tenant("vigo", "P1")
    return t


def _status(owner_engine):
    return sql(owner_engine, "SELECT status, handoff_reason FROM conversations")[0]


def test_a_human_reply_from_the_page_inbox_silences_the_agent(vigo, owner_engine):
    inbound("P1", "U1", "Bonjour", "m1")
    assert register_page_reply(_echo(app_id="9999")) == "human_takeover"
    assert _status(owner_engine) == ("human", "page_inbox")
    [(author, text_, status)] = sql(owner_engine, "SELECT author, text, delivery_status FROM messages WHERE direction = 'out'")
    assert (author, text_, status) == ("page_inbox", "Je m'en occupe.", "sent")

    inbound("P1", "U1", "Merci", "m2")                    # l'agent ne doit plus rien préparer
    assert sql(owner_engine, "SELECT count(*) FROM jobs WHERE kind = 'reply'")[0][0] == 1


def test_a_human_echo_without_app_id_is_detected_too(vigo, owner_engine):
    inbound("P1", "U1", "Bonjour", "m1")
    assert register_page_reply(_echo()) == "human_takeover"


def test_our_own_messages_echoed_back_are_ignored(vigo, owner_engine):
    inbound("P1", "U1", "Bonjour", "m1")
    sender = FakeSender()
    drain(FakeLLM(text_reply("Bonjour !")), sender)                       # l'agent répond (external_id = mid_out_1)
    assert register_page_reply(_echo(mid="mid_out_1")) == "own_echo"      # reconnu par l'identifiant du message
    assert register_page_reply(_echo(mid="autre_morceau")) == "own_echo"  # envoi très récent (message découpé)
    assert _status(owner_engine)[0] == "agent"


def test_echo_from_our_own_app_is_ignored(vigo, owner_engine):
    inbound("P1", "U1", "Bonjour", "m1")
    assert register_page_reply(_echo(app_id="1111")) == "own_echo"        # META_APP_ID du test
    assert _status(owner_engine)[0] == "agent"


def test_a_repeated_echo_is_absorbed_and_unknown_targets_ignored(vigo, owner_engine):
    inbound("P1", "U1", "Bonjour", "m1")
    assert register_page_reply(_echo(app_id="9999")) == "human_takeover"
    assert register_page_reply(_echo(app_id="9999")) == "own_echo"   # même identifiant déjà enregistré : absorbé
    assert register_page_reply(_echo(psid="INCONNU")) == "no_conversation"
    assert register_page_reply(_echo(page="PAGE_INCONNUE")) == "unknown_channel"
    assert sql(owner_engine, "SELECT count(*) FROM messages WHERE author = 'page_inbox'")[0][0] == 1


def test_parse_echoes_reads_the_customer_from_the_recipient():
    payload = build_payload("P1", "PAGE_AS_SENDER", "x")
    ev = payload["entry"][0]["messaging"][0]
    ev["sender"], ev["recipient"] = {"id": "P1"}, {"id": "U1"}
    ev["message"].update({"is_echo": True, "app_id": 1234})
    [echo] = parse_echoes(payload)
    assert (echo.customer_id, echo.channel_external_id, echo.app_id) == ("U1", "P1", "1234")
    assert parse_echoes(build_payload("P1", "U1", "message normal")) == []


def test_the_webhook_handles_echoes(vigo, owner_engine):
    inbound("P1", "U1", "Bonjour", "m1")
    payload = build_payload("P1", "x", "Je vous réponds ici")
    ev = payload["entry"][0]["messaging"][0]
    ev["sender"], ev["recipient"] = {"id": "P1"}, {"id": "U1"}
    ev["message"].update({"is_echo": True, "app_id": 9999})
    body = json.dumps(payload).encode()
    with TestClient(app) as c:
        r = c.post("/webhooks/messenger", content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": sign("test_secret", body)})
    assert r.status_code == 200
    assert _status(owner_engine) == ("human", "page_inbox")
