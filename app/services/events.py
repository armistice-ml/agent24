"""Événements temps réel : table `events` + NOTIFY PostgreSQL + diffusion SSE.

Principe : l'événement SIGNALE un changement (« la conversation X a changé »), il ne transporte
jamais de contenu. Le tableau de bord recharge ensuite via l'API, où l'autorisation est vérifiée.
Un événement perdu n'est donc pas grave : à la reconnexion le navigateur reprend (Last-Event-ID)
et le tableau de bord se resynchronise.
"""
import asyncio
import json
import logging
from uuid import UUID

from sqlalchemy import text

from app.config import get_settings

log = logging.getLogger(__name__)
CHANNEL = "agent24_events"
QUEUE_SIZE = 100


def emit(s, tenant_id: UUID, type: str, conversation_id: UUID | None = None, escalation_id: UUID | None = None) -> int:
    """À appeler DANS la transaction du changement : la notification part au commit, pas avant."""
    return s.execute(
        text(
            "INSERT INTO events (tenant_id, type, conversation_id, escalation_id) "
            "VALUES (:t, :ty, :c, :e) RETURNING id"
        ),
        {"t": tenant_id, "ty": type, "c": conversation_id, "e": escalation_id},
    ).scalar_one()


def public_event(payload: dict) -> dict:
    """Ce que le navigateur reçoit : jamais le tenant_id."""
    return {
        "id": payload.get("id"),
        "type": payload["type"],
        "conversation_id": payload.get("c"),
        "escalation_id": payload.get("e"),
    }


class EventHub:
    """Une connexion LISTEN par processus ; redistribue aux onglets connectés du BON client."""

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or get_settings().database_url.replace("postgresql+psycopg://", "postgresql://")
        self._subs: dict[UUID, set[asyncio.Queue]] = {}
        self.ready = asyncio.Event()

    def subscribe(self, tenant_id: UUID) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._subs.setdefault(tenant_id, set()).add(q)
        return q

    def unsubscribe(self, tenant_id: UUID, q: asyncio.Queue) -> None:
        subs = self._subs.get(tenant_id)
        if subs:
            subs.discard(q)
            if not subs:
                del self._subs[tenant_id]

    @staticmethod
    def _put(q: asyncio.Queue, event: dict) -> None:
        if q.full():  # abonné trop lent : on vide et on demande une resynchronisation complète
            while not q.empty():
                q.get_nowait()
            event = {"type": "resync"}
        q.put_nowait(event)

    def publish(self, payload: dict) -> None:
        try:
            tenant_id = UUID(payload["t"])
        except (KeyError, ValueError):
            return
        for q in list(self._subs.get(tenant_id, ())):
            self._put(q, public_event(payload))

    def publish_resync(self) -> None:
        for subs in self._subs.values():
            for q in list(subs):
                self._put(q, {"type": "resync"})

    async def run(self) -> None:
        import psycopg

        backoff = 1.0
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(self.dsn, autocommit=True) as conn:
                    await conn.execute(f"LISTEN {CHANNEL}")
                    self.ready.set()
                    backoff = 1.0
                    self.publish_resync()  # des événements ont pu être manqués pendant la coupure
                    async for notice in conn.notifies():
                        try:
                            self.publish(json.loads(notice.payload))
                        except json.JSONDecodeError:
                            log.warning("notification illisible ignorée")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("écoute des événements interrompue, nouvelle tentative dans %.0fs", backoff)
                self.ready.clear()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
