"""Flux SSE : signale les changements au tableau de bord (jamais de contenu de message)."""
import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from app.auth import AuthUser, current_user
from app.db import tenant_session
from app.services.events import EventHub, public_event

router = APIRouter(prefix="/api", tags=["events"])
PING_SECONDS = 20


def format_event(ev: dict) -> str:
    data = json.dumps({k: (str(v) if v is not None else None) for k, v in ev.items() if k != "id"})
    id_line = f"id: {ev['id']}\n" if ev.get("id") is not None else ""
    return f"{id_line}event: {ev['type']}\ndata: {data}\n\n"


def fetch_events_after(tenant_id: UUID, last_id: int, limit: int = 500) -> list[dict]:
    with tenant_session(tenant_id) as s:
        rows = s.execute(
            text("SELECT id, type, conversation_id AS c, escalation_id AS e FROM events WHERE id > :i ORDER BY id LIMIT :l"),
            {"i": last_id, "l": limit},
        ).mappings().all()
    return [public_event(dict(r)) for r in rows]


async def sse_stream(
    hub: EventHub,
    tenant_id: UUID,
    last_event_id: str | None,
    is_disconnected: Callable[[], Awaitable[bool]],
    ping_seconds: float = PING_SECONDS,
) -> AsyncIterator[str]:
    queue = hub.subscribe(tenant_id)  # abonnement AVANT le rattrapage : aucun trou possible
    try:
        yield "retry: 3000\n\n"
        last_sent = 0
        if last_event_id and last_event_id.isdigit():
            last_sent = int(last_event_id)
            for ev in await run_in_threadpool(fetch_events_after, tenant_id, last_sent):
                last_sent = max(last_sent, ev["id"])
                yield format_event(ev)
        else:
            yield format_event({"type": "ready"})
        while not await is_disconnected():
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=ping_seconds)
            except asyncio.TimeoutError:
                yield ": ping\n\n"  # garde la connexion ouverte à travers proxys et navigateurs
                continue
            if ev.get("id") is not None:
                if ev["id"] <= last_sent:  # déjà envoyé pendant le rattrapage
                    continue
                last_sent = ev["id"]
            yield format_event(ev)
    finally:
        hub.unsubscribe(tenant_id, queue)


@router.get("/events")
async def stream(request: Request, user: AuthUser = Depends(current_user)):
    generator = sse_stream(request.app.state.hub, user.tenant_id, request.headers.get("last-event-id"), request.is_disconnected)
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
