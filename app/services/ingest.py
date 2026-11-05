"""Enregistrement d'un message entrant : routage -> contact -> conversation -> message."""
import logging
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db import routing_session, tenant_session
from app.models import Contact, Conversation, Message
from app.schemas import NormalizedMessage
from app.services.events import emit

log = logging.getLogger(__name__)


@dataclass
class IngestResult:
    status: Literal["stored", "duplicate", "unknown_channel"]
    tenant_id: UUID | None = None
    conversation_id: UUID | None = None
    message_id: UUID | None = None


def resolve_route(channel_type: str, external_id: str) -> tuple[UUID, UUID] | None:
    with routing_session() as s:
        row = s.execute(
            text("SELECT tenant_id, channel_id FROM channel_routes WHERE type = :t AND external_id = :e"),
            {"t": channel_type, "e": external_id},
        ).first()
    return (row.tenant_id, row.channel_id) if row else None


def ingest(msg: NormalizedMessage) -> IngestResult:
    route = resolve_route(msg.channel_type, msg.channel_external_id)
    if route is None:
        log.warning("webhook pour un canal inconnu: %s/%s", msg.channel_type, msg.channel_external_id)
        return IngestResult(status="unknown_channel")
    tenant_id, channel_id = route

    with tenant_session(tenant_id) as s:
        # Contact (créé à la première apparition, sinon retrouvé)
        contact_id = s.execute(
            pg_insert(Contact)
            .values(tenant_id=tenant_id, channel_id=channel_id, channel_user_id=msg.channel_user_id)
            .on_conflict_do_update(
                index_elements=[Contact.tenant_id, Contact.channel_id, Contact.channel_user_id],
                set_={"channel_user_id": msg.channel_user_id},  # no-op pour obtenir l'id
            )
            .returning(Contact.id)
        ).scalar_one()

        # Conversation ouverte (une seule par contact, garantie par un index unique partiel)
        open_conv = select(Conversation.id, Conversation.status).where(
            Conversation.contact_id == contact_id, Conversation.status != "closed"
        )
        row = s.execute(open_conv).first()
        if row is None:
            row = s.execute(
                pg_insert(Conversation)
                .values(tenant_id=tenant_id, channel_id=channel_id, contact_id=contact_id, status="agent")
                .on_conflict_do_nothing()
                .returning(Conversation.id, Conversation.status)
            ).first()
            if row is None:  # course : une autre requête l'a créée entre-temps
                row = s.execute(open_conv).one()
        conversation_id, conversation_status = row

        # Message (idempotent : un renvoi par la plateforme ne crée pas de doublon)
        message_id = s.execute(
            pg_insert(Message)
            .values(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                channel_id=channel_id,
                direction="in",
                author="customer",
                type=msg.type,
                text=msg.text,
                media_ref=msg.media_ref,
                raw=msg.raw,
                idempotency_key=msg.idempotency_key,
                received_at=msg.received_at,
            )
            .on_conflict_do_nothing(index_elements=[Message.tenant_id, Message.channel_id, Message.idempotency_key])
            .returning(Message.id)
        ).scalar()

        if message_id is None:
            return IngestResult("duplicate", tenant_id, conversation_id)

        s.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(last_message_at=func.greatest(func.coalesce(Conversation.last_message_at, msg.received_at), msg.received_at))
        )

        emit(s, tenant_id, "message.received", conversation_id)

        # Outbox : le job est créé dans la MÊME transaction que le message. Si la conversation
        # est reprise par un humain, l'agent reste silencieux : pas de job.
        if conversation_status == "agent":
            s.execute(
                text("INSERT INTO jobs (tenant_id, conversation_id, message_id) VALUES (:t, :c, :m)"),
                {"t": tenant_id, "c": conversation_id, "m": message_id},
            )
    return IngestResult("stored", tenant_id, conversation_id, message_id)
