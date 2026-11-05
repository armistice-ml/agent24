import asyncio

from sqlalchemy import text

from app.db import tenant_session
from app.routers.events import format_event, sse_stream
from app.services.events import QUEUE_SIZE, EventHub, emit
from tests.helpers import inbound, sql


def _emit(tenant_id, type="message.received", conv=None):
    with tenant_session(tenant_id) as s:
        return emit(s, tenant_id, type, conv)


def test_events_are_isolated_by_tenant(make_tenant):
    ta, _ = make_tenant("a", "PA")
    tb, _ = make_tenant("b", "PB")
    _emit(ta)
    with tenant_session(tb) as s:
        assert s.execute(text("SELECT count(*) FROM events")).scalar() == 0
    with tenant_session(ta) as s:
        assert s.execute(text("SELECT count(*) FROM events")).scalar() == 1


def test_ingest_emits_an_event_without_message_content(make_tenant, owner_engine):
    t, _ = make_tenant("vigo", "P1")
    inbound("P1", "U1", "Contenu confidentiel", "m1")
    rows = sql(owner_engine, "SELECT type, conversation_id FROM events")
    assert [r[0] for r in rows] == ["message.received"] and rows[0][1] is not None
    assert "confidentiel" not in str(sql(owner_engine, "SELECT * FROM events"))


def test_listen_notify_reaches_only_the_right_tenant(make_tenant):
    ta, _ = make_tenant("a", "PA")
    tb, _ = make_tenant("b", "PB")

    async def scenario():
        hub = EventHub()
        task = asyncio.create_task(hub.run())
        await asyncio.wait_for(hub.ready.wait(), 5)
        qa, qb = hub.subscribe(ta), hub.subscribe(tb)
        await asyncio.get_running_loop().run_in_executor(None, _emit, ta, "escalation.created")
        ev = await asyncio.wait_for(qa.get(), 5)
        await asyncio.sleep(0.3)
        task.cancel()
        return ev, qb.empty()

    ev, other_is_empty = asyncio.run(scenario())
    assert ev["type"] == "escalation.created" and "t" not in ev and "tenant_id" not in ev
    assert other_is_empty


def test_a_slow_subscriber_gets_a_resync_instead_of_unbounded_memory():
    async def scenario():
        hub = EventHub("postgresql://inutile")
        from uuid import uuid4

        tenant = uuid4()
        q = hub.subscribe(tenant)
        for i in range(QUEUE_SIZE + 5):
            hub.publish({"id": i, "t": str(tenant), "type": "message.received"})
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        return items

    items = asyncio.run(scenario())
    # à la saturation la file est vidée et remplacée par « resync » ; les événements suivants viennent après
    assert items[0]["type"] == "resync" and len(items) <= QUEUE_SIZE
    assert [i["type"] for i in items].count("resync") == 1


def test_sse_stream_replays_missed_events_then_pings_and_cleans_up(make_tenant):
    t, _ = make_tenant("vigo", "P1")
    first = _emit(t)
    second = _emit(t, "conversation.updated")
    third = _emit(t, "escalation.created")

    async def scenario():
        hub = EventHub("postgresql://inutile")

        async def never_disconnected():
            return False

        chunks = []
        gen = sse_stream(hub, t, str(first), never_disconnected, ping_seconds=0.05)
        async for chunk in gen:
            chunks.append(chunk)
            if chunk.startswith(": ping"):
                break
        await gen.aclose()
        return chunks, dict(hub._subs)

    chunks, subs = asyncio.run(scenario())
    assert chunks[0] == "retry: 3000\n\n"
    assert chunks[1].startswith(f"id: {second}\nevent: conversation.updated")
    assert chunks[2].startswith(f"id: {third}\nevent: escalation.created")
    assert chunks[3] == ": ping\n\n"
    assert subs == {}                                   # désabonné à la fermeture


def test_sse_stream_does_not_resend_what_the_replay_already_sent(make_tenant):
    t, _ = make_tenant("vigo", "P1")
    first = _emit(t)
    second = _emit(t)

    async def scenario():
        hub = EventHub("postgresql://inutile")
        calls = {"n": 0}

        async def disconnected_after_two_polls():
            calls["n"] += 1
            return calls["n"] > 2

        # un événement « en direct » déjà rattrapé par le replay : doit être ignoré
        gen = sse_stream(hub, t, str(first), disconnected_after_two_polls, ping_seconds=0.05)
        out = []
        async for chunk in gen:
            out.append(chunk)
            if len(out) == 2:
                hub.publish({"id": second, "t": str(t), "type": "message.received"})
        return out

    out = asyncio.run(scenario())
    assert sum(1 for c in out if c.startswith(f"id: {second}\n")) == 1


def test_format_event_is_valid_sse_and_never_contains_the_tenant():
    chunk = format_event({"id": 7, "type": "message.received", "conversation_id": "abc", "escalation_id": None})
    assert chunk.startswith("id: 7\nevent: message.received\ndata: ") and chunk.endswith("\n\n")
    assert '"conversation_id": "abc"' in chunk and "tenant" not in chunk


def test_events_endpoint_requires_a_session(client):
    assert client.get("/api/events").status_code == 401
