import hashlib
import hmac

from app.channels.messenger import parse_payload, verify_signature
from scripts.send_test_webhook import build_payload


def _sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signature_valid_and_invalid():
    body = b'{"a":1}'
    assert verify_signature("s", body, _sig("s", body))
    assert not verify_signature("s", body, _sig("autre", body))
    assert not verify_signature("s", body, None)
    assert not verify_signature("s", body, "sha256=")
    assert not verify_signature("s", b'{"a":2}', _sig("s", body))  # corps modifié


def test_parse_text_message():
    [m] = parse_payload(build_payload("PAGE", "PSID", "Bonjour", mid="m_1"))
    assert (m.channel_external_id, m.channel_user_id, m.text, m.type) == ("PAGE", "PSID", "Bonjour", "text")
    assert m.idempotency_key == "messenger:m_1"
    assert m.received_at.tzinfo is not None


def test_parse_attachment():
    p = build_payload("PAGE", "PSID", "")
    msg = p["entry"][0]["messaging"][0]["message"]
    msg.pop("text")
    msg["attachments"] = [{"type": "audio", "payload": {"url": "https://cdn.example/a.mp4"}}]
    [m] = parse_payload(p)
    assert m.type == "audio" and m.media_ref == "https://cdn.example/a.mp4" and m.text is None


def test_parse_ignores_echo_delivery_read():
    p = build_payload("PAGE", "PSID", "x")
    p["entry"][0]["messaging"][0]["message"]["is_echo"] = True
    assert parse_payload(p) == []
    p2 = {"object": "page", "entry": [{"id": "PAGE", "messaging": [{"sender": {"id": "U"}, "delivery": {"mids": ["m"]}}]}]}
    assert parse_payload(p2) == []
