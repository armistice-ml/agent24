import json

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import tenant_session
from app.main import app
from scripts.send_test_webhook import build_payload, sign


def _post(client, payload, secret="test_secret"):
    body = json.dumps(payload).encode()
    return client.post(
        "/webhooks/messenger",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": sign(secret, body)},
    )


def test_verification_handshake():
    with TestClient(app) as c:
        ok = c.get("/webhooks/messenger", params={"hub.mode": "subscribe", "hub.verify_token": "test_verify", "hub.challenge": "42"})
        assert ok.status_code == 200 and ok.text == "42"
        bad = c.get("/webhooks/messenger", params={"hub.mode": "subscribe", "hub.verify_token": "faux", "hub.challenge": "42"})
        assert bad.status_code == 403


def test_valid_signature_stores_message(make_tenant):
    t, _ = make_tenant("vigo", "PAGE1")
    with TestClient(app) as c:
        r = _post(c, build_payload("PAGE1", "U1", "Bonjour", mid="m_1"))
        assert r.status_code == 200
        _post(c, build_payload("PAGE1", "U1", "Bonjour", mid="m_1"))  # renvoi par Meta
    with tenant_session(t) as s:
        assert s.execute(text("SELECT text FROM messages")).scalars().all() == ["Bonjour"]


def test_invalid_signature_rejected_and_nothing_stored(make_tenant):
    t, _ = make_tenant("vigo", "PAGE1")
    with TestClient(app) as c:
        r = _post(c, build_payload("PAGE1", "U1", "Bonjour"), secret="mauvais")
        assert r.status_code == 403
    with tenant_session(t) as s:
        assert s.execute(text("SELECT count(*) FROM messages")).scalar() == 0


def test_unknown_page_returns_200_but_stores_nothing():
    with TestClient(app) as c:
        assert _post(c, build_payload("INCONNUE", "U1", "Bonjour")).status_code == 200
