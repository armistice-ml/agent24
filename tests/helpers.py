from sqlalchemy import text

from app.channels.messenger import parse_payload
from app.services.ingest import ingest
from app.worker import run_once
from scripts.send_test_webhook import build_payload
from tests.fakes import FakeEmail


def inbound(page, psid, body, mid, kind="text"):
    payload = build_payload(page, psid, body, mid=mid)
    if kind != "text":
        m = payload["entry"][0]["messaging"][0]["message"]
        m.pop("text")
        m["attachments"] = [{"type": kind, "payload": {"url": "https://cdn.example/x"}}]
    return ingest(parse_payload(payload)[0])


def drain(llm, sender, email=None):
    email = email or FakeEmail()
    n = 0
    while run_once(llm, sender.factory(), email):
        n += 1
        assert n < 30
    return email


def conv_ids(owner_engine, tenant_id=None):
    sql = "SELECT id FROM conversations" + (" WHERE tenant_id = :t" if tenant_id else "") + " ORDER BY created_at"
    with owner_engine.connect() as c:
        return c.execute(text(sql), {"t": tenant_id} if tenant_id else {}).scalars().all()


def sql(owner_engine, query, **params):
    with owner_engine.begin() as c:
        res = c.execute(text(query), params)
        return res.all() if res.returns_rows else res.rowcount
